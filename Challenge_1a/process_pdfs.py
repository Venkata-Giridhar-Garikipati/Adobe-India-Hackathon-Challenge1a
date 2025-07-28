import fitz  # PyMuPDF
import json
import os
import re
import time
from collections import defaultdict

# Performance Tuning Parameters
MAX_PAGES = 50                    # Page limit (Adobe requirement: 50 pages max)
TIMEOUT_SECONDS = 10             # Processing timeout (Adobe requirement: 10 seconds max)
MIN_STYLE_FREQUENCY = 100        # Minimum style frequency for body text detection
MAX_HEADING_LENGTH = 250         # Maximum heading text length
TITLE_SEARCH_RATIO = 0.5         # Title search area (top half of first page)

def get_style_statistics(doc):
    style_counts = defaultdict(int)
    for page in doc:
        blocks = page.get_text("dict").get("blocks", [])
        for b in blocks:
            if "lines" in b:
                for l in b["lines"]:
                    for s in l["spans"]:
                        text = s.get('text', '').strip()
                        if text:
                            style_key = (round(s['size']), (s['flags'] & 2**4) > 0, s['font'])
                            style_counts[style_key] += len(text)
    return style_counts

def classify_heading_levels(style_counts):
    if not style_counts:
        return {}, None

    frequent_styles = {s: c for s, c in style_counts.items() if c > MIN_STYLE_FREQUENCY}
    body_style = max(frequent_styles, key=frequent_styles.get) if frequent_styles else max(style_counts, key=style_counts.get)
    body_size = body_style[0]

    heading_candidates = []
    for style, count in style_counts.items():
        size, is_bold, font = style
        if style != body_style and (size > body_size or (is_bold and size >= body_size)):
            if "italic" not in font.lower():
                heading_candidates.append(style)

    size_to_styles = defaultdict(list)
    for style in heading_candidates:
        size_to_styles[style[0]].append(style)

    distinct_sizes = sorted(size_to_styles.keys(), reverse=True)
    heading_map = {}
    levels = ["H1", "H2", "H3", "H4", "H5"]

    for i, size in enumerate(distinct_sizes):
        if i < len(levels):
            level = levels[i]
            for style in size_to_styles[size]:
                heading_map[style] = level

    return heading_map, body_style

def get_full_block_text(block):
    lines_text = []
    if "lines" in block:
        for line in block["lines"]:
            span_texts = [s['text'] for s in line["spans"] if s['text'].strip()]
            lines_text.append("".join(span_texts))
    return " ".join(lines_text).strip().replace("\u00a0", " ")

def classify_by_numbering(text):
    if re.match(r"^\d+\.\s", text):
        return "H1"
    if re.match(r"^\d+\.\d+\s", text):
        return "H2"
    if re.match(r"^\d+\.\d+\.\d+\s", text):
        return "H3"
    if re.match(r"^\d+\.\d+\.\d+\.\d+\s", text):
        return "H4"
    return None

def is_revision_or_version_header(text):
    if re.match(r"^\d+\.\d+\s+\d{1,2}\s+[A-Z]{3,9}\s+\d{4}\s+", text):
        return True
    if re.match(r"(?i)^version\s+\d+(\.\d+)?$", text.strip()):
        return True
    return False

def extract_outline(doc_path):
    start_time = time.time()
    try:
        doc = fitz.open(doc_path)
    except Exception as e:
        print(f"Error opening {doc_path}: {e}")
        return None

    if doc.page_count > MAX_PAGES:
        print(f"Skipping {doc_path} because it has more than {MAX_PAGES} pages.")
        return None

    style_counts = get_style_statistics(doc)
    heading_level_map, _ = classify_heading_levels(style_counts)

    outline_with_pos = []
    title = ""
    processed_bboxes = set()

    if doc.page_count > 0:
        first_page_blocks = doc[0].get_text("dict").get("blocks", [])
        max_size = 0
        title_style = None
        for b in first_page_blocks:
            if "lines" in b:
                for l in b["lines"]:
                    for s in l["spans"]:
                        if s['size'] > max_size:
                            max_size = s['size']
                            title_style = (round(s['size']), (s['flags'] & 2**4) > 0, s['font'])

        title_blocks = []
        if title_style:
            for b in first_page_blocks:
                if b['bbox'][1] > doc[0].rect.height * TITLE_SEARCH_RATIO:
                    continue
                if "lines" in b and b["lines"] and b["lines"][0]["spans"]:
                    s = b["lines"][0]["spans"][0]
                    block_style = (round(s['size']), (s['flags'] & 2**4) > 0, s['font'])
                    if block_style == title_style:
                        title_blocks.append(b)
                        processed_bboxes.add(b['bbox'])

        title_blocks.sort(key=lambda b: b['bbox'][1])
        title = " ".join([get_full_block_text(b) for b in title_blocks])

    for page_num, page in enumerate(doc):
        blocks = page.get_text("dict").get("blocks", [])
        for b in blocks:
            bbox = b['bbox']
            if not b.get("lines") or bbox in processed_bboxes:
                continue

            span = b["lines"][0]["spans"][0]
            block_style = (round(span['size']), (span['flags'] & 2**4) > 0, span['font'])
            block_text = get_full_block_text(b)
            if not block_text or len(block_text) > MAX_HEADING_LENGTH or block_text.strip().isdigit():
                continue
            if block_text.endswith('.') and len(block_text.split()) > 10:
                continue
            if re.match(r"^(January|February|March|April|May|June|July|August|September|October|November|December)\s+\d{1,2},\s+\d{4}$", block_text):
                continue
            if is_revision_or_version_header(block_text):
                continue

            numbered_level = classify_by_numbering(block_text)
            if numbered_level:
                level = numbered_level
            elif block_style in heading_level_map:
                level = heading_level_map[block_style]
            else:
                continue

            outline_with_pos.append({
                "level": level,
                "text": block_text,
                "page": page_num + 1,
                "y_pos": b['bbox'][1]
            })
            processed_bboxes.add(bbox)

    doc.close()

    level_priority = {"H1": 1, "H2": 2, "H3": 3, "H4": 4, "H5": 5}
    outline_with_pos.sort(key=lambda item: (level_priority.get(item['level'], 99), item['page'], item['y_pos']))

    final_outline = []
    seen_headings = set()
    for item in outline_with_pos:
        key = (item['text'], item['page'])
        if key not in seen_headings:
            final_outline.append({k: v for k, v in item.items() if k != 'y_pos'})
            seen_headings.add(key)

    if not title and final_outline:
        h1s = [h for h in final_outline if h['level'] == 'H1']
        if h1s:
            title = h1s[0]['text']

    elapsed = time.time() - start_time
    if elapsed > TIMEOUT_SECONDS:
        print(f"Warning: {doc_path} took {elapsed:.2f}s to process, which exceeds the {TIMEOUT_SECONDS}-second limit.")

    return {
        "title": " ".join(title.split()),
        "outline": final_outline
    }

def process_pdfs_in_directory(input_dir, output_dir):
    if not os.path.exists(output_dir):
        os.makedirs(output_dir)

    for filename in os.listdir(input_dir):
        if filename.lower().endswith(".pdf"):
            pdf_path = os.path.join(input_dir, filename)
            print(f"Processing {pdf_path}...")
            structured_outline = extract_outline(pdf_path)
            if structured_outline:
                base_filename = os.path.splitext(filename)[0]
                output_filename = f"{base_filename}.json"
                output_path = os.path.join(output_dir, output_filename)
                with open(output_path, 'w', encoding='utf-8') as f:
                    json.dump(structured_outline, f, indent=4, ensure_ascii=False)
                print(f"Successfully generated {output_path}")

if __name__ == "__main__":
    INPUT_DIRECTORY = "/app/input"
    OUTPUT_DIRECTORY = "/app/output"

    if not os.path.exists(INPUT_DIRECTORY):
        os.makedirs(INPUT_DIRECTORY)
        print(f"Created sample input directory: '{INPUT_DIRECTORY}'. Place PDFs here.")
    if not os.path.exists(OUTPUT_DIRECTORY):
        os.makedirs(OUTPUT_DIRECTORY)

    process_pdfs_in_directory(INPUT_DIRECTORY, OUTPUT_DIRECTORY)
