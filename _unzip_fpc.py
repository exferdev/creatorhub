import zipfile, os
DEST = "E:/creatorhub/data/fingerprint-chromium"
print("extracting...")
with zipfile.ZipFile(DEST + "/_dl.zip") as z:
    z.extractall(DEST)
os.remove(DEST + "/_dl.zip")
for root, dirs, files in os.walk(DEST):
    for fn in files:
        if fn.lower() in ("chrome.exe", "chromium.exe"):
            print("FOUND:", os.path.join(root, fn))
print("done")
