import asyncio
import json
import logging
import uuid
import soundcard as sc
from aiortc import RTCPeerConnection, RTCSessionDescription
from aiortc.contrib.media import MediaStreamTrack
from av import AudioFrame
import numpy as np
import websockets
import fractions
import threading
import tkinter as tk
#add
logging.basicConfig(level=logging.INFO)

class Config:
    SAMPLE_RATE = 48000
    FRAMES_PER_BUFFER = 960
    CHANNELS = 1

config = Config()

HOST = '0.0.0.0'
PORT = 8765

pcs = set()
pcs_lock = threading.Lock()

class AudioLoopbackTrack(MediaStreamTrack):
    kind = "audio"

    def __init__(self, config):
        super().__init__()
        self.config = config
        self.mic = sc.get_microphone(
            id=str(sc.default_speaker().name),
            include_loopback=True
        )
        self.recorder = None
        self.timestamp = 0

    async def recv(self):
        if self.recorder is None:
            self.recorder = self.mic.recorder(
                samplerate=self.config.SAMPLE_RATE,
                channels=self.config.CHANNELS,
                blocksize=self.config.FRAMES_PER_BUFFER
            )
            self.recorder.__enter__()
            logging.info("Audio recorder started.")

        try:
            data = self.recorder.record(numframes=self.config.FRAMES_PER_BUFFER)
            data = data.flatten()
            data = np.clip(data, -1.0, 1.0)
            data = (data * 32767).astype(np.int16)

            frame = AudioFrame(format='s16', layout='mono', samples=self.config.FRAMES_PER_BUFFER)
            frame.planes[0].update(data.tobytes())
            frame.sample_rate = self.config.SAMPLE_RATE
            frame.pts = self.timestamp
            frame.time_base = fractions.Fraction(1, self.config.SAMPLE_RATE)
            self.timestamp += self.config.FRAMES_PER_BUFFER
            return frame

        except Exception as e:
            logging.error(f"Error in recv: {e}")
            silent = np.zeros(self.config.FRAMES_PER_BUFFER, dtype=np.int16)
            frame = AudioFrame(format='s16', layout='mono', samples=self.config.FRAMES_PER_BUFFER)
            frame.planes[0].update(silent.tobytes())
            frame.sample_rate = self.config.SAMPLE_RATE
            frame.pts = self.timestamp
            frame.time_base = fractions.Fraction(1, self.config.SAMPLE_RATE)
            self.timestamp += self.config.FRAMES_PER_BUFFER
            return frame

    def stop(self):
        if self.recorder:
            self.recorder.__exit__(None, None, None)
            self.recorder = None
            logging.info("Audio recorder stopped.")
        super().stop()

async def websocket_handler(websocket):
    logging.info(f"Signaling client connected from {websocket.remote_address}")
    pc = RTCPeerConnection()
    pc_id = f"PeerConnection({uuid.uuid4()})"
    with pcs_lock:
        pcs.add(pc)

    audio_track = AudioLoopbackTrack(config)
    pc.addTrack(audio_track)

    @pc.on("connectionstatechange")
    async def on_connectionstatechange():
        logging.info(f"[{pc_id}] Connection state is {pc.connectionState}")
        if pc.connectionState == "failed" or pc.connectionState == "closed":
            await pc.close()
            with pcs_lock:
                pcs.discard(pc)
            logging.info(f"[{pc_id}] Connection closed.")

    try:
        async for message in websocket:
            data = json.loads(message)
            if data["type"] == "offer":
                offer = RTCSessionDescription(sdp=data["sdp"], type=data["type"])
                logging.info(f"[{pc_id}] Received offer, creating answer...")
                await pc.setRemoteDescription(offer)
                answer = await pc.createAnswer()
                await pc.setLocalDescription(answer)
                # Patch SDP to force ptime=10
                patched_sdp = pc.localDescription.sdp.replace("a=mid:audio\r\n", "a=mid:audio\r\na=ptime:10\r\n")
                response = {"type": "answer", "sdp": patched_sdp}
                await websocket.send(json.dumps(response))
                logging.info(f"[{pc_id}] Answer sent.")
            else:
                logging.warning(f"Unsupported message type: {data['type']}")
    except websockets.exceptions.ConnectionClosed:
        logging.info(f"Signaling client {websocket.remote_address} disconnected.")
    finally:
        with pcs_lock:
            if pc in pcs:
                await pc.close()
                pcs.discard(pc)

async def main(loop):
    logging.info(f"Starting signaling server on ws://{HOST}:{PORT}")
    logging.info("Run this script, then open the client HTML file in a browser.")
    logging.info("Find your PC's local IP address (e.g., ipconfig/ifconfig) for the client.")
    async with websockets.serve(websocket_handler, HOST, PORT):
        await asyncio.Future()  # Run forever

def start_server(loop):
    try:
        loop.run_until_complete(main(loop))
    except KeyboardInterrupt:
        logging.info("Server stopped by user.")

def close_all_connections(loop, status_var=None):
    async def close_all():
        with pcs_lock:
            to_close = list(pcs)
        for pc in to_close:
            await pc.close()
            with pcs_lock:
                pcs.discard(pc)
        logging.info("All peer connections closed due to parameter change.")
        if status_var:
            status_var.set("All clients forced to reconnect with new settings.")

    asyncio.run_coroutine_threadsafe(close_all(), loop)

def run_gui(loop):
    def apply_settings():
        try:
            sr = int(sample_rate_var.get())
            fpb = int(frames_per_buffer_var.get())
            ch = int(channels_var.get())
            if sr not in (8000, 16000, 24000, 32000, 44100, 48000):
                status_var.set("Sample rate should be a standard value (e.g., 48000)")
                return
            if fpb <= 0 or fpb > 4096:
                status_var.set("Frames per buffer should be 1-4096")
                return
            if ch not in (1, 2):
                status_var.set("Channels should be 1 (mono) or 2 (stereo)")
                return
            config.SAMPLE_RATE = sr
            config.FRAMES_PER_BUFFER = fpb
            config.CHANNELS = ch
            status_var.set(f"Applied: {sr} Hz, {fpb} frames, {ch} ch. Restarting connections...")
            close_all_connections(loop, status_var)
        except Exception as e:
            status_var.set(f"Error: {e}")

    root = tk.Tk()
    root.title("SoundShare Audio Settings")

    tk.Label(root, text="Sample Rate (Hz):").grid(row=0, column=0, sticky="e")
    sample_rate_var = tk.StringVar(value=str(config.SAMPLE_RATE))
    tk.Entry(root, textvariable=sample_rate_var, width=10).grid(row=0, column=1)

    tk.Label(root, text="Frames per Buffer:").grid(row=1, column=0, sticky="e")
    frames_per_buffer_var = tk.StringVar(value=str(config.FRAMES_PER_BUFFER))
    tk.Entry(root, textvariable=frames_per_buffer_var, width=10).grid(row=1, column=1)

    tk.Label(root, text="Channels:").grid(row=2, column=0, sticky="e")
    channels_var = tk.StringVar(value=str(config.CHANNELS))
    tk.Entry(root, textvariable=channels_var, width=10).grid(row=2, column=1)

    status_var = tk.StringVar(value="Adjust settings and click Apply.")
    tk.Label(root, textvariable=status_var, fg="blue").grid(row=3, column=0, columnspan=2, pady=5)

    tk.Button(root, text="Apply", command=apply_settings).grid(row=4, column=0, columnspan=2, pady=10)

    tk.Label(root, text="Tip: Lower frames per buffer = lower latency, higher = more stable.\n"
                        "Change settings and all clients will be forced to reconnect.",
             fg="gray", font=("Arial", 8)).grid(row=5, column=0, columnspan=2, pady=5)

    root.mainloop()

if __name__ == "__main__":
    # Create the main event loop in the main thread
    main_loop = asyncio.new_event_loop()
    threading.Thread(target=run_gui, args=(main_loop,), daemon=True).start()
    asyncio.set_event_loop(main_loop)
    start_server(main_loop)
