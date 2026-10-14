"""
check_vlm.py -- finds out in 10 seconds why FinMan is (not) calling the AI reader.

Put this next to app.py and run:   python check_vlm.py
Optional:                          python check_vlm.py path/to/a_bill.jpg
It reads the same .env that Flask reads, then tests each link in the chain
and says which one is broken. It never prints your full API key.
"""
import os, sys, io, json
import requests
from dotenv import load_dotenv, find_dotenv

path = find_dotenv(usecwd=True)
print(f".env file used      : {path or 'NOT FOUND (run this from the folder that has .env)'}")
load_dotenv(path)
url = (os.getenv("FINMAN_VLM_URL") or "").strip().rstrip("/")
key = (os.getenv("FINMAN_VLM_KEY") or "").strip()
print(f"FINMAN_VLM_URL      : {url or 'MISSING'}")
print(f"FINMAN_VLM_KEY      : {('set, %d characters, starts with %s...' % (len(key), key[:3])) if key else 'MISSING or EMPTY  <-- this alone makes FinMan silently skip the AI reader'}")
if not url or not key:
    sys.exit("\nFix .env first (no quotes, no spaces around '='), then restart Flask.")

H = {"X-API-Key": key, "ngrok-skip-browser-warning": "true"}

print("\n1) Is the Colab server reachable?  GET /health")
try:
    r = requests.get(url + "/health", headers=H, timeout=20)
except requests.exceptions.RequestException as e:
    sys.exit(f"   FAILED: {e}\n   -> Colab is stopped, or the URL is wrong. Re-run the last cell of the Phase 3 notebook.")
print(f"   HTTP {r.status_code}  {r.text[:150]!r}")
if r.status_code != 200 or "ok" not in r.text:
    sys.exit("   -> The tunnel answered, but not with the FinMan server. If you see 'ERR_NGROK', the Phase 3 cell is not running.")

print("\n2) Does it accept your key?  POST /parse")
if len(sys.argv) > 1:
    data, name, mime = open(sys.argv[1], "rb").read(), os.path.basename(sys.argv[1]), "image/jpeg"
else:
    from PIL import Image, ImageDraw, ImageFont
    im = Image.new("RGB", (900, 500), "white"); d = ImageDraw.Draw(im)
    try: f = ImageFont.truetype("DejaVuSans.ttf", 32)
    except Exception: f = ImageFont.load_default()
    for i, ln in enumerate(["TAX INVOICE", "Maju Trading Sdn. Bhd.", "Invoice No: INV-2026-00077", "Date: 12/03/2026", "TOTAL  RM 1,234.50"]):
        d.text((40, 30 + i * 80), ln, fill="black", font=f)
    b = io.BytesIO(); im.save(b, "PNG"); data, name, mime = b.getvalue(), "test.png", "image/png"
try:
    r = requests.post(url + "/parse?debug=true", headers=H, files={"file": (name, data, mime)},
                      data={"doc_type": "bill_received"}, timeout=(5, 180))
except requests.exceptions.RequestException as e:
    sys.exit(f"   FAILED: {e}")
print(f"   HTTP {r.status_code}")
if r.status_code == 401:
    sys.exit("   -> Key rejected. FINMAN_VLM_KEY in .env must be EXACTLY the value of the FINMAN_API_KEY secret in Colab.")
try:
    print(json.dumps(r.json(), indent=2))
except ValueError:
    print(r.text[:300])
print("\nIf step 2 printed JSON fields, the server side is fine. If FinMan still does not use it, restart Flask"
      " (the debug reloader does not re-read .env) and look for lines starting with [VLM] in the Flask terminal.")
