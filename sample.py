import os
import re
import math
import difflib
from dataclasses import dataclass
from pathlib import Path

import fitz  # PyMuPDF
from PIL import Image, ImageDraw

from azure.core.credentials import AzureKeyCredential
from azure.ai.formrecognizer import DocumentAnalysisClient

try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

# === Configuration ===
PDF_PATH = Path('filename.pdf')  # <-- set your PDF path
OUT_DIR = Path('output')
OUT_DIR.mkdir(parents=True, exist_ok=True)

DPI = 200  # render DPI

from azure.identity import ClientSecretCredential

AZURE_DI_ENDPOINT = "https://.cognitiveservices.azure.com/"
AZURE_TENANT_ID = ""
AZURE_CLIENT_ID = ""
AZURE_CLIENT_SECRET = ""

sp_credential = ClientSecretCredential(
    tenant_id=AZURE_TENANT_ID,
    client_id=AZURE_CLIENT_ID,
    client_secret=AZURE_CLIENT_SECRET,
)

client = DocumentAnalysisClient(
    endpoint=AZURE_DI_ENDPOINT,
    credential=sp_credential
)


print('Endpoint:', AZURE_DI_ENDPOINT)

# === Step 1: Render PDF pages to images ===

def render_pdf_to_images(pdf_path: Path, out_dir: Path, dpi: int = 200):
    out_dir.mkdir(parents=True, exist_ok=True)
    doc = fitz.open(pdf_path)
    zoom = dpi / 72.0
    mat = fitz.Matrix(zoom, zoom)
    image_paths = []

    for i in range(doc.page_count):
        page = doc.load_page(i)
        pix = page.get_pixmap(matrix=mat, alpha=False)
        img_path = out_dir / f'page_{i+1:04d}.png'
        pix.save(str(img_path))
        image_paths.append(img_path)

    return image_paths

image_paths = render_pdf_to_images(PDF_PATH, OUT_DIR / 'pages', dpi=DPI)
print('Rendered pages:', len(image_paths))

# === Step 2: OCR each image with Document Intelligence (prebuilt-read) ===
from tqdm.auto import tqdm

def analyze_read_ocr(image_path: Path):
    with open(image_path, 'rb') as f:
        poller = client.begin_analyze_document('prebuilt-read', document=f)
        return poller.result()

ocr_results = []
for p in tqdm(image_paths, desc='OCR pages'):
    ocr_results.append(analyze_read_ocr(p))

print('OCR done.')

for idx, p in enumerate(ocr_results):
    if 'UGI/Party ID' in p.content:
        print(f'Start redacting client details on Page: {idx + 1}')
    if 'PARTY DETAILS' in p.content:
        print(f'End redacting client details on Page: {idx + 1}')


idx_page_start = 2
for line in ocr_results[idx_page_start].to_dict()['pages'][0]['lines']:
    if 'UGI/Party ID' in line['content']:
        y_1 = min( point['y'] for point in line['polygon'])
        print('This is the start of redaction box...', y_1, '... for page ', idx_page_start+1)
    if 'GCMS/S' in line['content']:
        y_2 = min( point['y'] for point in line['polygon'])
        print('This is the end of redaction box...', y_2, '... for page ', idx_page_start+1)  

for line in ocr_results[idx_page_start+1].to_dict()['pages'][0]['lines']:
    if 'Request Date' in line['content']:
        y_3 = max( point['y'] for point in line['polygon'])
        print('This is the start of redaction box...', y_3, '... for page ', idx_page_start+2)
    if 'PARTY DETAILS' in line['content']:
        y_4 = min( point['y'] for point in line['polygon'])
        print('This is the end of redaction box...', y_4, '... for page ', idx_page_start+2)
        break

ugi_page = 2
party_page = 3
print('UGI page:', ugi_page+1, 'PARTY page:', party_page+1)
print('y_1:', y_1)
print('y_2:', y_2)
print('y_3:', y_3)
print('y_4:', y_4)


# === Step 4: Redact ===

def get_page_dims_from_ocr(res):
    page = res.pages[0]
    return float(page.width), float(page.height), getattr(page, 'unit', '')


def y_to_px(y, img_h, ocr_h):
    scale = img_h / ocr_h if ocr_h and ocr_h > 0 else 1.0
    return int(round(y * scale))


def redact_band(img: Image.Image, y_start_px: int, y_end_px: int, color=(0, 0, 0)):
    y0 = max(0, min(y_start_px, y_end_px))
    y1 = min(img.height, max(y_start_px, y_end_px))
    if y1 <= y0:
        return img
    draw = ImageDraw.Draw(img)
    draw.rectangle([0, y0, img.width, y1], fill=color)
    return img


redacted_dir = OUT_DIR / 'redacted'
redacted_dir.mkdir(parents=True, exist_ok=True)

images = [Image.open(p).convert('RGB') for p in image_paths]


# first page: y_1 -> y_2
_, ocr_h, _ = get_page_dims_from_ocr(ocr_results[ugi_page])
y0 = y_to_px(y_1, images[ugi_page].height, ocr_h)
y1 = y_to_px(y_2, images[ugi_page].height, ocr_h)
images[ugi_page] = redact_band(images[ugi_page], y0, y1)

# second page: y_3 -> y_4
_, ocr_h2, _ = get_page_dims_from_ocr(ocr_results[party_page])
y0b = y_to_px(y_3, images[party_page].height, ocr_h2)
y1b = y_to_px(y_4, images[party_page].height, ocr_h2)
images[party_page] = redact_band(images[party_page], y0b, y1b)

out_paths = []
for i, im in enumerate(images):
    out_path = redacted_dir / f'page_{i+1:04d}_redacted.png'
    im.save(out_path)
    out_paths.append(out_path)

print('Saved redacted images to:', redacted_dir)

# (Optional) Rebuild a redacted PDF
redacted_pdf_path = OUT_DIR / 'redacted_output.pdf'
imgs = [Image.open(p).convert('RGB') for p in out_paths]
if imgs:
    imgs[0].save(redacted_pdf_path, save_all=True, append_images=imgs[1:])
print('Redacted PDF:', redacted_pdf_path)

