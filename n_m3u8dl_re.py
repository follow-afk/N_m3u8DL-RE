import os
import sys
import argparse
import requests
import m3u8
import threading
import subprocess
import json
import re
import time
import concurrent.futures
from tqdm import tqdm
from urllib.parse import urljoin, urlparse
from Crypto.Cipher import AES
import xml.etree.ElementTree as ET

class MediaDownloader:
    def __init__(self, input_url, save_dir="downloads", save_name=None, thread_count=16, 
                 headers=None, keys=None, proxy=None, auto_select=False, use_shaka=False, live_pipe_mux=False):
        self.input_url = input_url
        self.save_dir = save_dir
        self.save_name = save_name or "output"
        self.thread_count = thread_count
        self.headers = headers or {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36'
        }
        self.keys = keys or []
        self.proxy = proxy
        self.auto_select = auto_select
        self.use_shaka = use_shaka
        self.live_pipe_mux = live_pipe_mux
        
        self.session = requests.Session()
        self.session.headers.update(self.headers)
        if self.proxy:
            self.session.proxies = {'http': self.proxy, 'https': self.proxy}
        
        self.tmp_dir = os.path.join(self.save_dir, "tmp_" + self.save_name)
        os.makedirs(self.save_dir, exist_ok=True)
        os.makedirs(self.tmp_dir, exist_ok=True)

    def log(self, level, msg):
        timestamp = time.strftime("%H:%M:%S", time.localtime())
        print(f"{timestamp} {level} : {msg}")

    def download_segment(self, url, path, pbar=None):
        if os.path.exists(path):
            if pbar: pbar.update(1)
            return True
        try:
            resp = self.session.get(url, timeout=15)
            resp.raise_for_status()
            with open(path, 'wb') as f:
                f.write(resp.content)
            if pbar: pbar.update(1)
            return True
        except Exception as e:
            if pbar: pbar.update(1)
            return False

    def decrypt_file(self, encrypted_path, decrypted_path):
        if not self.keys:
            os.rename(encrypted_path, decrypted_path)
            return True

        if self.use_shaka:
            cmd = ["shaka-packager", "--enable_raw_key_decryption"]
            for k in self.keys:
                if ":" in k:
                    kid, key = k.split(":")
                    cmd.extend(["--keys", f"key_id={kid}:key={key}"])
                else:
                    cmd.extend(["--keys", f"key={k}"])
            cmd.append(f"input={encrypted_path},stream=video,output={decrypted_path}")
        else:
            cmd = ["mp4decrypt"]
            for k in self.keys:
                cmd.extend(["--key", k])
            cmd.extend([encrypted_path, decrypted_path])
        
        try:
            subprocess.run(cmd, check=True, capture_output=True)
            return True
        except subprocess.CalledProcessError as e:
            self.log("ERROR", f"Decryption failed: {e.stderr.decode()}")
            return False

    def handle_dash(self):
        self.log("INFO", f"Loading URL: {self.input_url}")
        try:
            resp = self.session.get(self.input_url)
            resp.raise_for_status()
            mpd_text = resp.text
        except Exception as e:
            self.log("ERROR", f"Failed to load MPD: {e}")
            return

        if not mpd_text.strip():
            self.log("ERROR", "MPD content is empty. Check your proxy or URL.")
            return

        try:
            root = ET.fromstring(mpd_text)
        except ET.ParseError as e:
            self.log("ERROR", f"Failed to parse MPD XML: {e}")
            return

        # Handle namespaces
        ns = {'mpd': 'urn:mpeg:dash:schema:mpd:2011'}
        if 'xmlns' in root.attrib:
            ns['mpd'] = root.attrib['xmlns']
        
        self.log("INFO", "Content Matched: Dynamic Adaptive Streaming over HTTP")
        
        base_url_main = self.input_url.split('?')[0].rsplit('/', 1)[0] + '/'
        url_params = self.input_url.split('?', 1)[1] if '?' in self.input_url else ""

        adaptation_sets = root.findall('.//mpd:AdaptationSet', ns)
        if not adaptation_sets:
            # Try without namespace
            adaptation_sets = root.findall('.//AdaptationSet')
            ns = {}

        selected_reps = []
        for aset in adaptation_sets:
            reps = aset.findall('mpd:Representation', ns) if ns else aset.findall('Representation')
            if not reps: continue
            
            if self.auto_select:
                best_rep = max(reps, key=lambda r: int(r.get('bandwidth', 0)))
                selected_reps.append((aset, best_rep))

        for aset, rep in selected_reps:
            stype = aset.get('contentType') or ( 'video' if rep.get('width') else 'audio' )
            rid = rep.get('id')
            self.log("INFO", f"Selected {stype}: {rid} ({rep.get('bandwidth')} bps)")
            
            template = rep.find('mpd:SegmentTemplate', ns) if ns else rep.find('SegmentTemplate')
            if template is None:
                template = aset.find('mpd:SegmentTemplate', ns) if ns else aset.find('SegmentTemplate')
            
            if template is None: continue
            
            init_url = template.get('initialization').replace('$RepresentationID$', str(rid))
            full_init_url = urljoin(base_url_main, init_url)
            if url_params: full_init_url += ('&' if '?' in full_init_url else '?') + url_params
            
            init_path = os.path.join(self.tmp_dir, f"init_{stype}_{rid}.mp4")
            self.download_segment(full_init_url, init_path)
            
            seg_urls = []
            timeline = template.find('mpd:SegmentTimeline', ns) if ns else template.find('SegmentTimeline')
            if timeline is not None:
                current_time = 0
                s_elements = timeline.findall('mpd:S', ns) if ns else timeline.findall('S')
                for s in s_elements:
                    t = int(s.get('t', current_time))
                    d = int(s.get('d', 0))
                    r = int(s.get('r', 0))
                    for i in range(r + 1):
                        seg_url_part = template.get('media').replace('$RepresentationID$', str(rid)).replace('$Time$', str(t))
                        full_seg_url = urljoin(base_url_main, seg_url_part)
                        if url_params: full_seg_url += ('&' if '?' in full_seg_url else '?') + url_params
                        seg_urls.append(full_seg_url)
                        t += d
                    current_time = t
            
            seg_paths = []
            if seg_urls:
                with tqdm(total=len(seg_urls), desc=f"Downloading {stype}") as pbar:
                    with concurrent.futures.ThreadPoolExecutor(max_workers=self.thread_count) as executor:
                        futures = []
                        for i, url in enumerate(seg_urls):
                            path = os.path.join(self.tmp_dir, f"seg_{stype}_{rid}_{i:05d}.m4s")
                            seg_paths.append(path)
                            futures.append(executor.submit(self.download_segment, url, path, pbar))
                        concurrent.futures.wait(futures)
            
            merged_enc = os.path.join(self.tmp_dir, f"merged_{stype}_{rid}_enc.mp4")
            with open(merged_enc, 'wb') as outfile:
                if os.path.exists(init_path):
                    with open(init_path, 'rb') as infile: outfile.write(infile.read())
                for sp in seg_paths:
                    if os.path.exists(sp):
                        with open(sp, 'rb') as infile: outfile.write(infile.read())
            
            final_out = os.path.join(self.save_dir, f"{self.save_name}_{stype}.mp4")
            self.decrypt_file(merged_enc, final_out)
            self.log("INFO", f"Saved {stype} to {final_out}")

    def run(self):
        if ".mpd" in self.input_url.split('?')[0]:
            self.handle_dash()
        else:
            self.log("INFO", "HLS downloading not fully implemented in this rewrite yet.")

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
        live_pipe_mux=args.live_pipe_mux,
        headers=headers
    )
    downloader.run()

if __name__ == "__main__":
    main()
