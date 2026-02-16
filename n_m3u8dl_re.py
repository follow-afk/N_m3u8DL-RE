import os
import sys
import argparse
import requests
import m3u8
import threading
import subprocess
import json
import re
from tqdm import tqdm
from Crypto.Cipher import AES
from urllib.parse import urljoin, urlparse
import concurrent.futures
import time
from mpegdash.parser import MPEGDASHParser

class MediaDownloader:
    def __init__(self, input_url, save_dir="downloads", save_name=None, thread_count=16, 
                 headers=None, keys=None, proxy=None, auto_select=False, use_shaka=False):
        self.input_url = input_url
        self.save_dir = save_dir
        self.save_name = save_name or "output"
        self.thread_count = thread_count
        self.headers = headers or {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
        }
        self.keys = keys or []
        self.proxy = proxy
        self.auto_select = auto_select
        self.use_shaka = use_shaka
        self.session = requests.Session()
        self.session.headers.update(self.headers)
        if self.proxy:
            self.session.proxies = {'http': self.proxy, 'https': self.proxy}
        
        self.tmp_dir = os.path.join(self.save_dir, "tmp_" + self.save_name)
        if not os.path.exists(self.save_dir):
            os.makedirs(self.save_dir, exist_ok=True)
        if not os.path.exists(self.tmp_dir):
            os.makedirs(self.tmp_dir, exist_ok=True)

    def download_segment(self, url, path, pbar=None):
        if os.path.exists(path):
            if pbar: pbar.update(1)
            return True
        try:
            response = self.session.get(url, timeout=30)
            response.raise_for_status()
            with open(path, 'wb') as f:
                f.write(response.content)
            if pbar: pbar.update(1)
            return True
        except Exception as e:
            print(f"Error downloading {url}: {e}")
            return False

    def decrypt_mp4(self, input_path, output_path):
        if not self.keys:
            os.rename(input_path, output_path)
            return True

        if self.use_shaka:
            cmd = ["shaka-packager", "--enable_raw_key_decryption"]
            for k in self.keys:
                if ":" in k:
                    kid, key = k.split(":")
                    cmd.extend(["--keys", f"key_id={kid}:key={key}"])
                else:
                    cmd.extend(["--keys", f"key={k}"])
            cmd.append(f"input={input_path},stream=video,output={output_path}")
        else:
            cmd = ["mp4decrypt"]
            for k in self.keys:
                cmd.extend(["--key", k])
            cmd.extend([input_path, output_path])
        
        try:
            subprocess.run(cmd, check=True, capture_output=True)
            return True
        except subprocess.CalledProcessError as e:
            print(f"Decryption failed: {e.stderr.decode()}")
            return False

    def handle_hls(self):
        print(f"Analyzing HLS: {self.input_url}")
        response = self.session.get(self.input_url)
        playlist = m3u8.loads(response.text, uri=self.input_url)
        
        if playlist.is_variant:
            best_stream = max(playlist.playlists, key=lambda p: p.stream_info.bandwidth)
            self.input_url = urljoin(self.input_url, best_stream.uri)
            playlist = m3u8.loads(self.session.get(self.input_url).text, uri=self.input_url)

        segments = playlist.segments
        total = len(segments)
        
        with tqdm(total=total, desc="Downloading HLS") as pbar:
            with concurrent.futures.ThreadPoolExecutor(max_workers=self.thread_count) as executor:
                futures = []
                for i, seg in enumerate(segments):
                    seg_url = urljoin(self.input_url, seg.uri)
                    target = os.path.join(self.tmp_dir, f"seg_{i:05d}.ts")
                    futures.append(executor.submit(self.download_hls_segment, seg_url, seg.key, target, i, pbar))
                concurrent.futures.wait(futures)
        
        self.merge_files(sorted([f for f in os.listdir(self.tmp_dir) if f.startswith("seg_")]), self.save_name + ".ts")

    def download_hls_segment(self, url, key_info, path, index, pbar):
        if os.path.exists(path):
            pbar.update(1)
            return
        
        response = self.session.get(url)
        data = response.content
        if key_info:
            key_url = urljoin(url, key_info.uri)
            key = self.session.get(key_url).content
            iv = bytes.fromhex(key_info.iv.replace('0x', '')) if key_info.iv else index.to_bytes(16, 'big')
            cipher = AES.new(key, AES.MODE_CBC, iv=iv)
            data = cipher.decrypt(data)
        
        with open(path, 'wb') as f:
            f.write(data)
        pbar.update(1)

    def handle_dash(self):
        print(f"Analyzing DASH: {self.input_url}")
        response = self.session.get(self.input_url)
        mpd = MPEGDASHParser.parse(response.text)
        
        period = mpd.periods[0]
        # Select best video adaptation set
        video_sets = [s for s in period.adaptation_sets if s.content_type == 'video' or (not s.content_type and any(r.width for r in s.representations))]
        if not video_sets:
            print("No video adaptation set found.")
            return
        
        v_set = video_sets[0]
        best_rep = max(v_set.representations, key=lambda r: r.bandwidth)
        
        base_url = self.input_url.rsplit('/', 1)[0] + '/'
        if best_rep.base_urls:
            base_url = urljoin(base_url, best_rep.base_urls[0].base_url_value)
        elif v_set.base_urls:
            base_url = urljoin(base_url, v_set.base_urls[0].base_url_value)

        template = best_rep.segment_templates[0] if best_rep.segment_templates else v_set.segment_templates[0]
        
        # Initialization
        init_url = template.initialization.replace('$RepresentationID$', str(best_rep.id))
        init_url = urljoin(base_url, init_url)
        init_path = os.path.join(self.tmp_dir, "init.mp4")
        print(f"Downloading initialization: {init_url}")
        self.download_segment(init_url, init_path)

        # Segments
        seg_urls = []
        if template.segment_timelines:
            timeline = template.segment_timelines[0]
            current_time = 0
            for s in timeline.s:
                t = s.t if s.t is not None else current_time
                d = s.d
                r = s.r if s.r is not None else 0
                for i in range(r + 1):
                    seg_url = template.media.replace('$RepresentationID$', str(best_rep.id)).replace('$Time$', str(t))
                    seg_urls.append(urljoin(base_url, seg_url))
                    t += d
                current_time = t
        else:
            # Fallback to $Number$
            start_number = template.start_number if template.start_number is not None else 1
            # For VOD, we might need to calculate the number of segments
            # This is a simplified fallback
            for i in range(start_number, start_number + 100): # Limit to 100 for safety if no timeline
                seg_url = template.media.replace('$RepresentationID$', str(best_rep.id)).replace('$Number$', str(i))
                seg_urls.append(urljoin(base_url, seg_url))

        print(f"Total segments to download: {len(seg_urls)}")
        with tqdm(total=len(seg_urls), desc="Downloading DASH") as pbar:
            with concurrent.futures.ThreadPoolExecutor(max_workers=self.thread_count) as executor:
                futures = []
                for i, url in enumerate(seg_urls):
                    target = os.path.join(self.tmp_dir, f"seg_{i:05d}.m4s")
                    futures.append(executor.submit(self.download_segment, url, target, pbar))
                concurrent.futures.wait(futures)

        # Merge
        files = ["init.mp4"] + sorted([f for f in os.listdir(self.tmp_dir) if f.startswith("seg_")])
        temp_merged = os.path.join(self.tmp_dir, "merged_encrypted.mp4")
        self.merge_files(files, temp_merged, is_abs=True)
        
        # Decrypt
        final_output = os.path.join(self.save_dir, self.save_name + ".mp4")
        print("Decrypting...")
        if self.decrypt_mp4(temp_merged, final_output):
            print(f"Successfully saved to {final_output}")
        else:
            print(f"Saved encrypted file to {temp_merged}")

    def merge_files(self, files, output_name, is_abs=False):
        output_path = output_name if is_abs else os.path.join(self.save_dir, output_name)
        with open(output_path, 'wb') as outfile:
            for f in files:
                f_path = f if is_abs else os.path.join(self.tmp_dir, f)
                if os.path.exists(f_path):
                    with open(f_path, 'rb') as infile:
                        outfile.write(infile.read())
        
    def run(self):
        if ".mpd" in self.input_url.split('?')[0]:
            self.handle_dash()
        else:
            self.handle_hls()

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("input")
    parser.add_argument("--save-name", default="output")
    parser.add_argument("--key", action="append")
    parser.add_argument("--proxy")
    parser.add_argument("--auto-select", action="store_true")
    parser.add_argument("--use-shaka-packager", action="store_true")
    parser.add_argument("--live-pipe-mux", action="store_true")
    parser.add_argument("-H", "--header", action="append")
    
    args = parser.parse_args()
    
    headers = {}
    if args.header:
        for h in args.header:
            if ":" in h:
                k, v = h.split(":", 1)
                headers[k.strip()] = v.strip()

    downloader = MediaDownloader(
        input_url=args.input,
        save_name=args.save_name,
        keys=args.key,
        proxy=args.proxy,
        auto_select=args.auto_select,
        use_shaka=args.use_shaka_packager,
        headers=headers
    )
    downloader.run()

if __name__ == "__main__":
    main()
