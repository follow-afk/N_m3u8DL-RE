import os
import sys
import argparse
import requests
import m3u8
import threading
import subprocess
import json
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
        self.keys = keys or [] # List of KID:KEY or KEY
        self.proxy = proxy
        self.auto_select = auto_select
        self.use_shaka = use_shaka
        self.session = requests.Session()
        self.session.headers.update(self.headers)
        if self.proxy:
            self.session.proxies = {'http': self.proxy, 'https': self.proxy}
        
        self.tmp_dir = os.path.join(self.save_dir, "tmp_" + self.save_name)
        if not os.path.exists(self.save_dir):
            os.makedirs(self.save_dir)
        if not os.path.exists(self.tmp_dir):
            os.makedirs(self.tmp_dir)

    def download_file(self, url, path):
        response = self.session.get(url, timeout=30, stream=True)
        response.raise_for_status()
        with open(path, 'wb') as f:
            for chunk in response.iter_content(chunk_size=8192):
                f.write(chunk)

    def decrypt_mp4(self, encrypted_path, decrypted_path):
        if not self.keys:
            os.rename(encrypted_path, decrypted_path)
            return

        if self.use_shaka:
            # shaka-packager --enable_raw_key_decryption --keys key_id=KID:key=KEY input=in.mp4,stream=video,output=out.mp4
            key_args = []
            for k in self.keys:
                if ":" in k:
                    kid, key = k.split(":")
                    key_args.append(f"key_id={kid}:key={key}")
                else:
                    key_args.append(f"key={k}")
            
            cmd = ["shaka-packager", "--enable_raw_key_decryption"]
            for ka in key_args:
                cmd.extend(["--keys", ka])
            cmd.append(f"input={encrypted_path},stream=video,output={decrypted_path}")
        else:
            # mp4decrypt --key KID:KEY in.mp4 out.mp4
            cmd = ["mp4decrypt"]
            for k in self.keys:
                cmd.extend(["--key", k])
            cmd.extend([encrypted_path, decrypted_path])
        
        try:
            subprocess.run(cmd, check=True, capture_output=True)
        except subprocess.CalledProcessError as e:
            print(f"Decryption failed: {e.stderr.decode()}")
            os.rename(encrypted_path, decrypted_path)

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
        
        self.merge_ts()

    def download_hls_segment(self, url, key_info, path, index, pbar):
        if os.path.exists(path):
            pbar.update(1)
            return
        
        response = self.session.get(url)
        data = response.content
        if key_info:
            # Handle AES-128
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
        
        # Simple selection: first period, first video adaptation set, highest bandwidth representation
        period = mpd.periods[0]
        video_set = [s for s in period.adaptation_sets if s.content_type == 'video' or not s.content_type][0]
        best_rep = max(video_set.representations, key=lambda r: r.bandwidth)
        
        base_url = self.input_url.rsplit('/', 1)[0] + '/'
        if best_rep.base_urls:
            base_url = urljoin(base_url, best_rep.base_urls[0].base_url_value)

        # Handle SegmentTemplate
        template = best_rep.segment_templates[0] if best_rep.segment_templates else video_set.segment_templates[0]
        
        init_url = urljoin(base_url, template.initialization.replace('$RepresentationID$', best_rep.id))
        init_path = os.path.join(self.tmp_dir, "init.mp4")
        self.download_file(init_url, init_path)

        # Download segments
        # Note: This is a simplified DASH downloader for VOD
        # Real implementation would need to handle SegmentTimeline
        print("Downloading DASH segments...")
        # For simplicity, we assume a fixed number of segments or use a loop
        # In a real tool, we'd parse SegmentTimeline
        
        # Placeholder for merging and decrypting
        # Since we can't easily parse all DASH types without a full engine, 
        # we provide the structure for the user to extend.
        print("DASH downloading is partially implemented. Merging init and segments...")
        
        final_encrypted = os.path.join(self.tmp_dir, "encrypted.mp4")
        # Combine init and segments into final_encrypted
        # self.decrypt_mp4(final_encrypted, os.path.join(self.save_dir, self.save_name + ".mp4"))

    def merge_ts(self):
        output = os.path.join(self.save_dir, self.save_name + ".ts")
        files = sorted([f for f in os.listdir(self.tmp_dir) if f.startswith("seg_")])
        with open(output, 'wb') as outfile:
            for f in files:
                with open(os.path.join(self.tmp_dir, f), 'rb') as infile:
                    outfile.write(infile.read())
        print(f"Saved to {output}")

    def run(self):
        if ".mpd" in self.input_url:
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
