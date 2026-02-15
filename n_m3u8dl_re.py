import os
import sys
import argparse
import requests
import m3u8
import threading
from tqdm import tqdm
from Crypto.Cipher import AES
from urllib.parse import urljoin
import concurrent.futures
import time

class M3U8Downloader:
    def __init__(self, input_url, save_dir="downloads", save_name=None, thread_count=16, headers=None):
        self.input_url = input_url
        self.save_dir = save_dir
        self.save_name = save_name or "output"
        self.thread_count = thread_count
        self.headers = headers or {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
        }
        self.session = requests.Session()
        self.session.headers.update(self.headers)
        self.tmp_dir = os.path.join(self.save_dir, "tmp_" + self.save_name)
        
        if not os.path.exists(self.save_dir):
            os.makedirs(self.save_dir)
        if not os.path.exists(self.tmp_dir):
            os.makedirs(self.tmp_dir)

    def get_m3u8_content(self, url):
        response = self.session.get(url)
        response.raise_for_status()
        return m3u8.loads(response.text, uri=url)

    def decrypt_data(self, data, key, iv):
        if not key:
            return data
        cipher = AES.new(key, AES.MODE_CBC, iv=iv)
        return cipher.decrypt(data)

    def download_segment(self, seg_index, seg_url, key_info, progress_bar):
        try:
            target_path = os.path.join(self.tmp_dir, f"seg_{seg_index:05d}.ts")
            if os.path.exists(target_path):
                progress_bar.update(1)
                return True

            response = self.session.get(seg_url, timeout=30)
            response.raise_for_status()
            data = response.content

            if key_info:
                key_url = urljoin(seg_url, key_info.uri)
                key_response = self.session.get(key_url)
                key_response.raise_for_status()
                key = key_response.content
                iv = bytes.fromhex(key_info.iv.replace('0x', '')) if key_info.iv else seg_index.to_bytes(16, 'big')
                data = self.decrypt_data(data, key, iv)

            with open(target_path, 'wb') as f:
                f.write(data)
            
            progress_bar.update(1)
            return True
        except Exception as e:
            print(f"\nError downloading segment {seg_index}: {e}")
            return False

    def download(self):
        print(f"Analyzing {self.input_url}...")
        playlist = self.get_m3u8_content(self.input_url)

        if playlist.is_variant:
            print("Variant playlist detected. Selecting the best stream...")
            # Simple selection: highest bandwidth
            best_stream = max(playlist.playlists, key=lambda p: p.stream_info.bandwidth)
            playlist_url = urljoin(self.input_url, best_stream.uri)
            playlist = self.get_m3u8_content(playlist_url)
        else:
            playlist_url = self.input_url

        segments = playlist.segments
        total_segments = len(segments)
        print(f"Total segments: {total_segments}")

        with tqdm(total=total_segments, desc="Downloading") as pbar:
            with concurrent.futures.ThreadPoolExecutor(max_workers=self.thread_count) as executor:
                futures = []
                for i, segment in enumerate(segments):
                    seg_url = urljoin(playlist_url, segment.uri)
                    key_info = segment.key
                    futures.append(executor.submit(self.download_segment, i, seg_url, key_info, pbar))
                
                concurrent.futures.wait(futures)

        self.merge_segments()

    def merge_segments(self):
        print("Merging segments...")
        output_file = os.path.join(self.save_dir, f"{self.save_name}.ts")
        segment_files = sorted([f for f in os.listdir(self.tmp_dir) if f.startswith("seg_")])
        
        with open(output_file, 'wb') as outfile:
            for seg_file in segment_files:
                seg_path = os.path.join(self.tmp_dir, seg_file)
                with open(seg_path, 'rb') as infile:
                    outfile.write(infile.read())
        
        print(f"Successfully saved to {output_file}")
        # Clean up
        for seg_file in segment_files:
            os.remove(os.path.join(self.tmp_dir, seg_file))
        os.rmdir(self.tmp_dir)

def main():
    parser = argparse.ArgumentParser(description="Python implementation of N_m3u8DL-RE core functionality")
    parser.add_argument("input", help="M3U8 URL or local file")
    parser.add_argument("--save-dir", default="downloads", help="Directory to save the downloaded file")
    parser.add_argument("--save-name", help="Name of the output file (without extension)")
    parser.add_argument("--thread-count", type=int, default=16, help="Number of download threads")
    parser.add_argument("-H", "--header", action="append", help="HTTP headers (e.g., -H \"Cookie: abc\")")

    args = parser.parse_args()

    headers = {}
    if args.header:
        for h in args.header:
            if ":" in h:
                k, v = h.split(":", 1)
                headers[k.strip()] = v.strip()

    downloader = M3U8Downloader(
        input_url=args.input,
        save_dir=args.save_dir,
        save_name=args.save_name,
        thread_count=args.thread_count,
        headers=headers
    )
    downloader.download()

if __name__ == "__main__":
    main()
