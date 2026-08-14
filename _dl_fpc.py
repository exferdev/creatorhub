"""下载并解压 fingerprint-chromium Windows 版"""
import requests, zipfile, os, io

URL = ("https://github.com/adryfish/fingerprint-chromium/releases/download/"
       "148.0.7778.215/ungoogled-chromium_148.0.7778.215-1.1_windows_x64.zip")
DEST = "E:/creatorhub/data/fingerprint-chromium"

os.makedirs(DEST, exist_ok=True)
print("downloading...")
r = requests.get(URL, stream=True, timeout=600)
total = int(r.headers.get("content-length", 0))
done = 0
with open("E:/creatorhub/data/fingerprint-chromium/_dl.zip", "wb") as f:
    for chunk in r.iter_content(1024 * 1024):
        f.write(chunk)
        done += len(chunk)
        if total:
            print(f"\r{done // 1024 // 1024}/{total // 1024 // 1024} MB", end="")
print("\ndownload done, extracting...")
with zipfile.ZipFile("E:/creatorhub/data/fingerprint-chromium/_dl.zip") as z:
    z.extractall(DEST)
os.remove("E:/creatorhub/data/fingerprint-chromium/_dl.zip")
# 找 chrome.exe
for root, dirs, files in os.walk(DEST):
    for fn in files:
        if fn.lower() in ("chrome.exe", "chromium.exe"):
            print("FOUND:", os.path.join(root, fn))
print("done")
