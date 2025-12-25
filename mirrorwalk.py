import os
import json
import csv
import hashlib
import pytesseract
import subprocess
import platform
import time
from PIL import Image
from concurrent.futures import ProcessPoolExecutor, as_completed
from thefuzz import fuzz

os.environ['OMP_THREAD_LIMIT'] = '1'
os.environ['MKL_NUM_THREADS'] = '1'

# Configuration
DATA_FILE = 'results.json'
CSV_FILE = 'results.csv'
BATCH_SIZE = 25  # Save a specified number of images at a time while processing to avoid disk-save hangs

def get_image_hash(filepath):
    hasher = hashlib.md5()
    try:
        with open(filepath, 'rb') as f:
            buf = f.read()
            hasher.update(buf)
        return hasher.hexdigest()
    except Exception:
        return None

def open_file(path):
    try:
        if platform.system() == "Windows":
            os.startfile(path)
        elif platform.system() == "Darwin": 
            subprocess.call(["open", path])
        else: 
            subprocess.call(["xdg-open", path], stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
    except Exception as e:
        print(f"Could not open file: {e}", flush=True)

def ocr_worker(img_info):
    img_path, file_hash = img_info
    filename = os.path.basename(img_path)
    start_time = time.time()
    try:
        custom_config = r'--oem 3 --psm 3' 
        with Image.open(img_path) as img:
            img = img.convert('L') 
            text = pytesseract.image_to_string(img, config=custom_config)
        
        elapsed = time.time() - start_time
        return file_hash, {
            "filename": filename,
            "full_path": os.path.abspath(img_path),
            "text": text.strip().replace('\n', ' '),
            "time": round(elapsed, 2)
        }
    except Exception as e:
        return file_hash, {"filename": filename, "full_path": os.path.abspath(img_path), "text": f"Error: {e}", "time": 0}

def save_results(data):
    with open(DATA_FILE, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=4)
    with open(CSV_FILE, 'w', newline='', encoding='utf-8') as f:
        writer = csv.writer(f)
        writer.writerow(['Filename', 'Path', 'Extracted Text'])
        for info in data.values():
            writer.writerow([info['filename'], info.get('full_path', ''), info['text']])

def process_images(IMAGE_FOLDER, recursive=False):
    try:
        pytesseract.get_tesseract_version()
    except Exception:
        print("\n[!] ERROR: Tesseract engine not found.", flush=True)
        return

    existing_data = {}
    if os.path.exists(DATA_FILE):
        try:
            with open(DATA_FILE, 'r', encoding='utf-8') as f:
                existing_data = json.load(f)
        except: pass

    valid_ext = ('.png', '.jpg', '.jpeg', '.tiff', '.bmp')
    all_files = []
    
    scan_msg = "(including subfolders)" if recursive else "(top-level only)"
    print(f"--- Scanning folder {scan_msg} ---", flush=True)
    
    if recursive:
        for root, _, files in os.walk(IMAGE_FOLDER):
            for f in files:
                if f.lower().endswith(valid_ext): all_files.append(os.path.join(root, f))
    else:
        for f in os.listdir(IMAGE_FOLDER):
            if f.lower().endswith(valid_ext): all_files.append(os.path.join(IMAGE_FOLDER, f))

    tasks = []
    for idx, path in enumerate(all_files):
        if idx % 100 == 0:
            print(f"Hashing: {idx}/{len(all_files)}...", end='\r', flush=True)
        
        f_hash = get_image_hash(path)
        if f_hash and f_hash not in existing_data:
            tasks.append((path, f_hash))
        elif f_hash:
            existing_data[f_hash]["full_path"] = os.path.abspath(path)

    print(f"\nScan Complete. {len(tasks)} new images to process.", flush=True)

    if not tasks:
        save_results(existing_data)
        return

    print(f"--- Processing {len(tasks)} images in parallel ---", flush=True)
    
    processed_count = 0
    num_workers = max(1, os.cpu_count() - 1) 
    
    with ProcessPoolExecutor(max_workers=num_workers) as executor:
        futures = {executor.submit(ocr_worker, task): task for task in tasks}
        
        for future in as_completed(futures):
            processed_count += 1
            try:
                f_hash, result = future.result()
                existing_data[f_hash] = result
                print(f"[{processed_count}/{len(tasks)}] DONE: {result['filename']} ({result['time']}s)", flush=True)
            except Exception as e:
                print(f"Error on image: {e}", flush=True)
            
            if processed_count % BATCH_SIZE == 0:
                save_results(existing_data)

    save_results(existing_data)
    print(f"\n--- Finished! Total indexed: {len(existing_data)} ---", flush=True)

def search_index():
    if not os.path.exists(DATA_FILE):
        print("No index found.", flush=True)
        return
    
    query = input("\nEnter text to search: ").lower()
    
    fuzzy_choice = input("Enable fuzzy search? (y/n) >>> ").lower().strip()
    use_fuzzy = True if fuzzy_choice == 'y' else False

    with open(DATA_FILE, 'r', encoding='utf-8') as f:
        data = json.load(f)
    
    results = []
    for info in data.values():
        text_body = info['text'].lower()
        
        if query in text_body:
            pos = text_body.find(query)
            start_idx = max(0, pos - 30)
            end_idx = min(len(text_body), pos + len(query) + 30)
            snippet = info['text'][start_idx:end_idx].strip().replace('\n', ' ')
            results.append((info, 100, snippet))
        
        elif use_fuzzy:
            score = fuzz.partial_ratio(query, text_body)
            if score >= 70:
                snippet = info['text'][:60].strip().replace('\n', ' ') + "..."
                results.append((info, score, snippet))
    
    results.sort(key=lambda x: x[1], reverse=True)
    
    if results:
        while True:
            mode_label = "Fuzzy + Exact" if use_fuzzy else "Exact Only"
            print(f"\n--- Search Results [{mode_label}] for: '{query}' ({len(results)} found) ---", flush=True)
            for i, (info, score, snippet) in enumerate(results):
                label = "[Exact]" if score == 100 else f"[{score}%]"
                print(f"[{i + 1}] {info['filename']} {label}")
                print(f"    Context: \"...{snippet}...\"")
            
            print("-" * 30)
            print("Options: [Number] to open | 'e' to export list | [Enter] to exit")
            choice = input(">>> ").lower().strip()
            
            if choice == "":
                break 
            
            if choice == 'e':
                export_file = "search_results.txt"
                with open(export_file, 'w', encoding='utf-8') as ef:
                    ef.write(f"Search Results for: {query} ({mode_label})\n")
                    ef.write("="*40 + "\n")
                    for info, score, snippet in results:
                        ef.write(f"File: {info['filename']}\n")
                        ef.write(f"Path: {info['full_path']}\n")
                        ef.write(f"Text: {info['text']}\n")
                        ef.write("-" * 20 + "\n")
                print(f"\n[!] Results exported to {export_file}", flush=True)
                continue

            if choice.isdigit():
                idx = int(choice) - 1
                if 0 <= idx < len(results):
                    file_to_open = results[idx][0]['full_path']
                    print(f"Opening: {results[idx][0]['filename']}...", flush=True)
                    open_file(file_to_open)
                else:
                    print(f"Invalid range (1-{len(results)}).", flush=True)
            else:
                print("Invalid input.", flush=True)
    else:
        print(f"\nNo matches found using {mode_label if 'mode_label' in locals() else 'search'}.", flush=True)

def main():
    while True:
        print("\n--- ADVANCED IMAGE SEARCH ENGINE ---")
        print("1. Process/Re-index Folder")
        print("2. Search and Open")
        print("3. Exit")
        choice = input("Select an option (1-3): ")
        if choice == '1':
            folder = input("Please input the path to folder containing images >>> ")
            sub_choice = input("Include subfolders? (y/n) >>> ").lower()
            recursive = True if sub_choice == 'y' else False
            process_images(folder, recursive)
        elif choice == '2': 
            search_index()
        elif choice == '3': 
            break

if __name__ == "__main__":
    main()
