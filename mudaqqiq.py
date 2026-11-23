# -*- coding: utf-8 -*-
"""
مُدقِّق الطابعي — أداة تدقيق الملازم أوفلاين
------------------------------------------------
- تدقيق إملائي/ترقيم/حروف كبيرة/قواعد بسيطة للنص الإنكليزي (LanguageTool محلي)
- تدقيق إملائي عربي أولي (LanguageTool محلي، نتائجه تحتاج تأكيد)
- مطابقة النصوص الإنكليزية المنقولة مع الكتاب الرسمي (OCR بـTesseract)
- تقرير PDF/HTML: الجملة كاملة، الخطأ بالأحمر، التصحيح بالأخضر، لوگو بالخلفية

التشغيل:
    python mudaqqiq.py                 ← واجهة رسومية
    python mudaqqiq.py --malzama M.pdf --book PB.pdf --book AB.pdf [--logo L.png] [--out DIR]
"""
import argparse, base64, difflib, hashlib, html, json, os, re, shutil, socket, subprocess
import sys, tempfile, threading, time, unicodedata
from collections import Counter
from datetime import datetime

APP_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(os.environ.get('LOCALAPPDATA') or os.path.expanduser('~/.local/share'), 'Mudaqqiq')
CACHE_DIR = os.path.join(DATA_DIR, 'cache')
CONFIG_PATH = os.path.join(APP_DIR, 'config.json')
LT_PORT = 8081
VERSION = '1.4'

DEFAULT_CONFIG = {
    "tesseract": "",          # يتعبى من التنصيب
    "tessdata": "",
    "java": "",
    "languagetool_dir": "",
    "browser": "",            # msedge.exe أو chrome.exe لتحويل التقرير إلى PDF
    "logo": "",
    "english_variant": "en-GB",
    "check_arabic": False,     # تجريبي: الـPDF يشوّه العربي فتطلع إنذارات كاذبة
    "ocr_dpi": 170,
    # أسماء الشخصيات والأماكن اللي ما نعدها أخطاء إملائية (تنضاف لها كلمات الكتاب تلقائياً)
    "extra_words": ["Yousif", "Karim", "Lina", "Nadia", "Tariq", "Rami", "Adel", "Hadi", "Amy",
                    "Alex", "Baghdad", "Basra", "Mosul", "Erbil", "Jadriya", "Mansur", "Euphrates",
                    "Tigris", "Iraq", "Iraqi", "IQD", "RE", "PE"],
    # قواعد LanguageTool المعطلة لأنها تطلع إنذارات كاذبة بالملازم
    "disabled_rules_en": ["WHITESPACE_RULE", "EN_QUOTES", "DASH_RULE", "COMMA_PARENTHESIS_WHITESPACE",
                          "ENGLISH_WORD_REPEAT_BEGINNING_RULE", "EN_UNPAIRED_BRACKETS",
                          "PUNCTUATION_PARAGRAPH_END", "SENTENCE_WHITESPACE", "DOUBLE_PUNCTUATION",
                          "EN_UNPAIRED_QUOTES", "ARROWS", "MULTIPLICATION_SIGN", "PLUS_MINUS"],
    "disabled_rules_ar": ["WHITESPACE_RULE", "COMMA_PARENTHESIS_WHITESPACE", "DOUBLE_PUNCTUATION",
                          "UNPAIRED_BRACKETS", "AR_HOMOPHONES_HAMZA"]
}


def load_config():
    cfg = dict(DEFAULT_CONFIG)
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, encoding='utf-8') as f:
                cfg.update(json.load(f))
        except Exception:
            pass
    return cfg


def log_default(msg):
    print(msg, flush=True)


# ---------------------------------------------------------------- PDF backend
class PDF:
    """قراءة الـPDF: PyMuPDF إذا موجود، وإلا Poppler (pdftotext/pdftoppm)."""

    def __init__(self, path):
        self.path = path
        try:
            try:
                import pymupdf as fitz  # PyMuPDF (الاسم الجديد)
            except ImportError:
                import fitz
            self.doc = fitz.open(path)
            self.backend = 'fitz'
            self.pages = self.doc.page_count
        except ImportError:
            if os.name == 'nt':
                raise RuntimeError('PyMuPDF is not installed. Run INSTALL.bat again.')
            self.backend = 'poppler'
            out = subprocess.run(['pdfinfo', path], capture_output=True, text=True).stdout
            self.pages = int(re.search(r'Pages:\s+(\d+)', out).group(1))

    def text(self, i):  # i يبدأ من 1
        if self.backend == 'fitz':
            return self.doc[i - 1].get_text('text', sort=True)
        return subprocess.run(['pdftotext', '-f', str(i), '-l', str(i), '-layout', self.path, '-'],
                              capture_output=True, text=True).stdout

    def image(self, i, dpi):
        from PIL import Image
        if self.backend == 'fitz':
            import io
            pix = self.doc[i - 1].get_pixmap(dpi=dpi)
            return Image.open(io.BytesIO(pix.tobytes('png')))
        tmp = tempfile.mkdtemp()
        subprocess.run(['pdftoppm', '-f', str(i), '-l', str(i), '-r', str(dpi), '-gray', '-png',
                        self.path, os.path.join(tmp, 'p')], check=True)
        f = [x for x in os.listdir(tmp) if x.endswith('.png')][0]
        img = Image.open(os.path.join(tmp, f)); img.load()
        shutil.rmtree(tmp, ignore_errors=True)
        return img


def file_hash(path):
    h = hashlib.sha1()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(1 << 20), b''):
            h.update(chunk)
    return h.hexdigest()[:16]


# ---------------------------------------------------------------- normalisation
RTL_MARKS = re.compile('[\u200e\u200f\u202a-\u202e\u2066-\u2069\ufeff]')
AR = re.compile('[\u0600-\u06FF]')
APOS = str.maketrans({'’': "'", '‘': "'", '´': "'", '`': "'", '“': '"', '”': '"'})


def normalize(t):
    t = unicodedata.normalize('NFKC', t)
    t = RTL_MARKS.sub('', t).replace('\u0640', '')  # الكشيدة
    return t.translate(APOS)


def is_arabic_line(s):
    a = len(AR.findall(s)); l = len(re.findall('[A-Za-z]', s))
    return a > l


LATIN_CHUNK = re.compile(r"[A-Za-z][A-Za-z0-9'\.,\?!:;\-\(\)/ ]*[A-Za-z0-9\.\?!\)]|[A-Za-z]")


def english_chunks(line):
    """يرجّع المقاطع الإنكليزية بالسطر (حتى لو مخلوط عربي)."""
    out = []
    for m in LATIN_CHUNK.finditer(line):
        s = re.sub(r'\s{2,}', '  ', m.group(0)).strip()
        # تقسيم الأعمدة: مسافتين أو أكثر = عمود جديد
        for part in re.split(r'\s{2,}', s):
            part = part.strip()
            if len(re.findall('[A-Za-z]', part)) >= 2:
                out.append(part)
    return out


def arabic_chunks(line):
    out = []
    for part in re.split(r'\s{3,}', line):
        part = part.strip()
        if len(AR.findall(part)) >= 6:
            # نشيل المقاطع الإنكليزية حتى ما يدققها المدقق العربي
            part = re.sub(r'[A-Za-z][A-Za-z0-9\'\.\?!,\- ]*', ' ', part)
            part = re.sub(r'\s+', ' ', part).strip()
            if part:
                out.append(part)
    return out


# ---------------------------------------------------------------- unit / lesson
NUM_WORDS = {'one': 1, 'two': 2, 'three': 3, 'four': 4, 'five': 5, 'six': 6, 'seven': 7, 'eight': 8,
             'nine': 9, 'ten': 10}


def detect_structure(texts):
    unit, lesson, res = 0, 0, {}
    for p, t in enumerate(texts, 1):
        m = re.search(r'\bunit\s*([0-9]+|[a-z]+)\b', t, re.I)
        if m:
            v = m.group(1).lower()
            v = int(v) if v.isdigit() else NUM_WORDS.get(v)
            if v:
                unit, lesson = v, 0
        m = re.search(r'\blesson\s*([0-9]+)', t, re.I)
        if m:
            lesson = int(m.group(1))
        res[p] = (unit, lesson)
    return res


# ---------------------------------------------------------------- LanguageTool
class LanguageTool:
    def __init__(self, cfg, log):
        self.cfg, self.log, self.proc = cfg, log, None
        self.url = f'http://127.0.0.1:{LT_PORT}/v2/check'

    def _up(self):
        try:
            with socket.create_connection(('127.0.0.1', LT_PORT), timeout=1):
                return True
        except OSError:
            return False

    def start(self):
        if self._up():
            return True
        lt = self.cfg.get('languagetool_dir')
        java = self.cfg.get('java') or 'java'
        jar = os.path.join(lt, 'languagetool-server.jar') if lt else ''
        if not jar or not os.path.exists(jar):
            self.log('! LanguageTool not found - skipping spell check.')
            return False
        flags = 0x08000000 if os.name == 'nt' else 0  # CREATE_NO_WINDOW
        self.proc = subprocess.Popen([java, '-Xmx2g', '-cp', jar, 'org.languagetool.server.HTTPServer',
                                      '--port', str(LT_PORT)], cwd=lt, stdout=subprocess.DEVNULL,
                                     stderr=subprocess.DEVNULL, creationflags=flags)
        for _ in range(90):
            if self._up():
                time.sleep(1)
                return True
            time.sleep(1)
        self.log('! LanguageTool did not start within 90s - skipping spell check.')
        return False

    def stop(self):
        if self.proc:
            self.proc.terminate()

    def check(self, text, lang, disabled):
        import requests
        data = {'text': text, 'language': lang, 'disabledRules': ','.join(disabled)}
        if lang.startswith('en'):
            data['motherTongue'] = 'ar'
        for attempt in range(3):
            try:
                r = requests.post(self.url, data=data, timeout=120)
                r.raise_for_status()
                return r.json().get('matches', [])
            except Exception as e:
                if attempt == 2:
                    self.log(f'! LanguageTool error: {e}')
                time.sleep(2)
        return []


CAT_AR = {'TYPOS': 'خطأ إملائي', 'CASING': 'حرف كبير/صغير', 'PUNCTUATION': 'علامة ترقيم',
          'GRAMMAR': 'خطأ قواعد', 'CONFUSED_WORDS': 'كلمة مستخدمة بغير مكانها',
          'REDUNDANCY': 'تكرار', 'TYPOGRAPHY': 'طباعة', 'COLLOCATIONS': 'تركيب غير صحيح',
          'MISC': 'ملاحظة'}


# ---------------------------------------------------------------- book OCR
def _tess_env(cfg):
    """يرجّع (tesseract_cmd, tessdata_prefix). نفضّل نموذج eng اللي جاي ويه تنصيب Tesseract
    لأنه أسرع بكثير من tessdata_best."""
    cmd = cfg.get('tesseract') or shutil.which('tesseract') or 'tesseract'
    inst = os.path.join(os.path.dirname(cmd), 'tessdata')
    if os.path.exists(os.path.join(inst, 'eng.traineddata')):
        return cmd, inst
    td = cfg.get('tessdata') or ''
    return cmd, td if td and os.path.exists(os.path.join(td, 'eng.traineddata')) else ''


def ocr_book(path, cfg, log, progress=None):
    """OCR لكتاب سكنر مرة وحدة. الرسم بالـthread الرئيسي (PyMuPDF ما يدعم threads)،
    والـOCR بـTesseract على عدة threads بنفس الوقت (كل وحدة تشغّل tesseract.exe مستقل)."""
    import threading
    from concurrent.futures import ThreadPoolExecutor, as_completed
    import pytesseract
    os.makedirs(CACHE_DIR, exist_ok=True)
    cache = os.path.join(CACHE_DIR, f'book_{file_hash(path)}.json')
    partial = cache + '.partial'
    if os.path.exists(cache):
        with open(cache, encoding='utf-8') as f:
            log('   (cached - no OCR needed)')
            return json.load(f)
    done = {}
    if os.path.exists(partial):
        try:
            with open(partial, encoding='utf-8') as f:
                done = {int(k): v for k, v in json.load(f).items()}
        except Exception:
            done = {}
    cmd, prefix = _tess_env(cfg)
    if not os.path.exists(cmd) and not shutil.which(cmd):
        raise RuntimeError(f'Tesseract not found: {cmd}')
    pytesseract.pytesseract.tesseract_cmd = cmd
    os.environ['OMP_THREAD_LIMIT'] = '1'
    if prefix:
        os.environ['TESSDATA_PREFIX'] = prefix
    pdf = PDF(path)
    n = pdf.pages
    dpi = cfg.get('ocr_dpi', 170)
    lock = threading.Lock()

    def work(i):
        with lock:  # PyMuPDF: رسم صفحة وحدة بكل مرة
            t = normalize(pdf.text(i))
            if len(re.findall('[A-Za-z]', t)) >= 40:
                return i, t, None
            img = pdf.image(i, dpi).convert('L')
        try:
            return i, normalize(pytesseract.image_to_string(img, lang='eng', config='--oem 1 --psm 3')), None
        except Exception as e:
            return i, '', f'{type(e).__name__}: {e}'

    todo = [i for i in range(1, n + 1) if i not in done]
    workers = max(1, (os.cpu_count() or 2) - 1)
    log(f'   OCR: {len(todo)} pages, {workers} parallel (resumes if interrupted)')
    t0, errors = time.time(), 0
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = [ex.submit(work, i) for i in todo]
        for k, fu in enumerate(as_completed(futs), 1):
            i, t, err = fu.result()
            if err:
                errors += 1
                if errors == 1:
                    log(f'   ! OCR error on page {i}: {err}')
                if errors >= 5 and len(done) == 0:
                    ex.shutdown(cancel_futures=True)
                    raise RuntimeError(f'Tesseract failed: {err}')
            done[i] = t
            if progress:
                progress(len(done), n)
            if k % 10 == 0 or k == len(todo):
                with open(partial, 'w', encoding='utf-8') as f:
                    json.dump(done, f, ensure_ascii=False)
                el = time.time() - t0
                log(f'   OCR {len(done)}/{n}  (~{int(el / k * (len(todo) - k))}s left)')
    pages = [done.get(i, '') for i in range(1, n + 1)]
    with open(cache, 'w', encoding='utf-8') as f:
        json.dump(pages, f, ensure_ascii=False)
    try:
        os.remove(partial)
    except OSError:
        pass
    return pages


WORD = re.compile(r"\d+(?:st|nd|rd|th)\b|[A-Za-z]+(?:'[A-Za-z]+)?|\d+(?:[.,]\d+)*")


def toks(s):
    return WORD.findall(s)


class BookIndex:
    def __init__(self, books):  # books = [(name, [page texts])]
        self.sents = []  # (tokens_lower, original, book_name, printed_page)
        self.vocab = Counter()
        self.inv = {}
        self.ctx = {}  # (w1,w2,w3) -> Counter(next word) ؛ ('R',w2,w3,w4) -> Counter(previous word)
        self.where = {}
        for name, pages in books:
            for i, t in enumerate(pages, 1):
                tk = [w.lower() for w in toks(t)]
                for w in tk:
                    self.vocab[w] += 1
                for k in range(len(tk) - 3):
                    a = tuple(tk[k:k + 3]); self.ctx.setdefault(a, Counter())[tk[k + 3]] += 1
                    b = ('R',) + tuple(tk[k + 1:k + 4]); self.ctx.setdefault(b, Counter())[tk[k]] += 1
                    self.where.setdefault(a + (tk[k + 3],), (name, i - 1))
                # نجمع الأسطر المتتالية حتى الجمل المكسورة على سطرين تنطابق
                lines = [l.strip() for l in t.split('\n')]
                buf = []
                for l in lines + ['']:
                    if l:
                        buf.append(l)
                        continue
                    block = ' '.join(buf); buf = []
                    for s in re.split(r'(?<=[\.\?!])\s+', block):
                        tl = [w.lower() for w in toks(s)]
                        if len(tl) >= 4:
                            idx = len(self.sents)
                            self.sents.append((tl, s, name, i - 1))
                            for w in set(tl):
                                self.inv.setdefault(w, []).append(idx)

    def real(self, w):
        return self.vocab[w.lower()] >= 2

    def best(self, tl):
        cand = Counter()
        for w in set(tl):
            for idx in self.inv.get(w, [])[:400]:
                cand[idx] += 1
        best, br = None, 0
        for idx, c in cand.most_common(40):
            if c < 3:
                break
            st = self.sents[idx][0]
            sm = difflib.SequenceMatcher(None, tl, st, autojunk=False)
            # نقيس التطابق نسبةً لجملة الملزمة (حتى لو جملة الكتاب أطول)
            m = sum(b.size for b in sm.get_matching_blocks())
            r = m / max(len(tl), 1)
            if r > br:
                best, br = idx, r
        return best, br


# ---------------------------------------------------------------- checks
def check_against_book(malz_sentences, index, log):
    """يقارن كل جملة إنكليزية بالملزمة بأقرب جملة بالكتاب ويطلّع الفروقات الحقيقية."""
    out = []
    for page, sent in malz_sentences:
        tl_orig = toks(sent)
        tl = [w.lower() for w in tl_orig]
        if len(tl) < 6 or re.match(r'^\s*[A-Za-z][\-\.\)]\s', sent):
            continue
        idx, r = index.best(tl)
        if idx is None or r < 0.8:
            continue
        bt, bsent, bname, bpage = index.sents[idx]
        sm = difflib.SequenceMatcher(None, tl, bt, autojunk=False)
        # نحصر المقارنة بجزء جملة الكتاب المقابل
        blocks = [b for b in sm.get_matching_blocks() if b.size]
        if not blocks:
            continue
        j0 = blocks[0].b; j1 = blocks[-1].b + blocks[-1].size
        sm = difflib.SequenceMatcher(None, tl, bt[j0:j1], autojunk=False)
        btok = toks(bsent)[j0:j1]
        ops = [o for o in sm.get_opcodes() if o[0] != 'equal']
        if len(ops) > 2:  # جملة مختلفة كثير = تمرين بنفس القالب، مو نقل غلط
            continue
        for op, i1, i2, k1, k2 in ops:
            mw = tl_orig[i1:i2]; bw = btok[k1:k2]
            # لازم الكلمات قبل وبعد الفرق متطابقة ويه الكتاب (سياق مؤكد)
            if i1 < 2 or (op != 'replace' and i2 > len(tl) - 1):
                continue
            if op == 'replace':
                if len(mw) == 1 and len(bw) == 2 and ''.join(bw).lower() == mw[0].lower():
                    kind = 'كلمتين ملتصقتين'
                    bw = [' '.join(bw)]
                    corrected = _rebuild(sent, tl_orig, i1, i2, bw)
                    out.append(_mk(page, sent, corrected, mw, bw, kind, bname, bpage))
                    continue
                if len(mw) != 1 or len(bw) != 1:
                    continue
                a, b = mw[0], bw[0]
                va, vb = index.vocab[a.lower()], index.vocab[b.lower()]
                if a.isdigit() and b.isdigit() and index.vocab[b] >= 2:
                    kind = 'رقم مختلف عن الكتاب'
                elif a.isalpha() and b.isalpha() and len(b) >= 3 and index.real(b) and \
                        difflib.SequenceMatcher(None, a.lower(), b.lower()).ratio() >= 0.7 and a.lower() != b.lower() and vb > va:
                    kind = 'كلمة منقولة بشكل مختلف عن الكتاب'
                else:
                    continue
            elif op == 'delete':  # كلمة زايدة بالملزمة
                if len(mw) > 1 or index.vocab[mw[0].lower()] > 0:
                    continue
                kind = 'كلمة غريبة غير موجودة بالكتاب'
            else:  # insert: كلمة ساقطة
                if len(bw) != 1 or not bw[0].isalpha() or len(bw[0]) < 2 or bw[0].lower() in ('and', 'ey', 'ie', 'oa') or index.vocab[bw[0].lower()] < 5 or r < 0.9:
                    continue
                kind = 'كلمة ساقطة من النص المنقول'
            corrected = _rebuild(sent, tl_orig, i1, i2, bw)
            out.append(_mk(page, sent, corrected, mw, bw, kind, bname, bpage))
    return out


def check_ngrams(en_by_page, index):
    """يكشف الكلمة المنقولة غلط حتى لو الـOCR خلط أعمدة الكتاب: يعتمد على سياق 3 كلمات
    قبل الكلمة أو بعدها، على نص الصفحة كلها (حتى الجملة المكسورة على سطرين تنفحص)."""
    out = []
    for page, chunks in en_by_page.items():
        stream = []  # (lower, original, chunk_idx, idx_in_chunk)
        for ci, ch in enumerate(chunks):
            for ti, w in enumerate(toks(ch)):
                stream.append((w.lower(), w, ci, ti))
        tk = [x[0] for x in stream]
        for i, (w, ow, ci, ti) in enumerate(stream):
            if not w.isalpha() or len(w) < 4:
                continue
            for key in ([tuple(tk[i - 3:i])] if i >= 3 else []) + \
                       ([('R',) + tuple(tk[i + 1:i + 4])] if i + 3 < len(tk) else []):
                cont = index.ctx.get(key)
                if not cont or w in cont or sum(cont.values()) > 6:
                    continue
                x, c = cont.most_common(1)[0]
                if not x.isalpha() or len(x) < 3 or index.vocab[x] <= index.vocab[w] or index.vocab[x] < 3:
                    continue
                if x.startswith(w) or w.startswith(x) or x.endswith(w) or w.endswith(x) or sorted(x) == sorted(w) or x[:4] == w[:4] and len(x) > 4 and len(w) > 4:
                    continue  # تصريف ثاني لنفس الكلمة (stay/stayed) مو خطأ
                if difflib.SequenceMatcher(None, w, x).ratio() < 0.7:
                    continue
                fix = x.capitalize() if ow[0].isupper() else x
                sent = chunks[ci]
                corrected = _rebuild(sent, toks(sent), ti, ti + 1, [fix])
                ref = index.where.get(key + (x,)) if key[0] != 'R' else None
                out.append(_mk(page, sent, corrected, [ow], [fix], 'كلمة منقولة بشكل مختلف عن الكتاب',
                               *(ref or ('الكتاب', '?'))))
                break
    return out


def _mk(page, sent, corrected, mw, bw, kind, bname, bpage):
    wrong, fixed = ' '.join(mw), ' '.join(bw)
    return {'page': page, 'type': 'مطابقة الكتاب', 'lang': 'en', 'location': '',
            'original': sent, 'corrected': corrected,
            'wrong': [wrong] if wrong else [], 'fixed': [fixed] if fixed else [],
            'note': f'{kind}: بالكتاب «{fixed or "—"}» وبالملزمة «{wrong or "—"}».',
            'ref': f'{bname} ص{bpage}' if bpage != '?' else 'الكتاب الرسمي', 'severity': 'عالي' if kind.startswith('رقم') else 'متوسط',
            'confirm': True}


def _rebuild(sent, tl, i1, i2, repl):
    """يبدّل الكلمات i1:i2 بالجملة الأصلية مع الحفاظ على الترقيم."""
    spans = [m.span() for m in WORD.finditer(sent)]
    r = ' '.join(repl)
    if i1 < i2:
        a, b = spans[i1][0], spans[i2 - 1][1]
        return sent[:a] + r + sent[b:]
    pos = spans[i1][0] if i1 < len(spans) else len(sent)
    return sent[:pos] + r + ' ' + sent[pos:]


def lt_findings(lt, pages_chunks, lang, cfg, allowed_words):
    """pages_chunks = [(page, [chunks])] — تنفحص كلها بطلب واحد (أسرع بكثير من طلب لكل صفحة)."""
    flat = [(pg, c) for pg, cs in pages_chunks for c in cs]
    if not flat:
        return []
    chunks = [c for _, c in flat]
    text = '\n\n'.join(chunks)
    starts, p = [], 0
    for c in chunks:
        starts.append(p); p += len(c) + 2
    disabled = cfg['disabled_rules_en'] if lang.startswith('en') else cfg['disabled_rules_ar']
    res = []
    import bisect
    for m in lt.check(text, lang, disabled):
        off, ln = m['offset'], m['length']
        k = bisect.bisect_right(starts, off) - 1
        page = flat[k][0]
        chunk = chunks[k]; o = off - starts[k]
        if o + ln > len(chunk):
            continue
        bad = chunk[o:o + ln]
        if not bad.strip():
            continue
        cat = m.get('rule', {}).get('category', {}).get('id', 'MISC')
        rid = m.get('rule', {}).get('id', '')
        reps = [r['value'] for r in m.get('replacements', [])][:1]
        if lang.startswith('en'):
            if cat == 'TYPOS' and (bad.lower() in allowed_words or bad.isupper() or len(bad) <= 2):
                continue
            if rid == 'UPPERCASE_SENTENCE_START' and (re.match(r'^[a-z]\s*[\.\)]', chunk) or len(toks(chunk)) < 3):
                continue  # ترقيم a. b. c. أو كلمات مفردة بجداول وصناديق
        else:
            if cat not in ('TYPOS', 'GRAMMAR', 'MISC'):
                continue
            # تشوهات استخراج "لا" المعروفة
            if re.search(r'ال[ءأإآ]|اال|^ال$', bad):
                continue
        if not reps:
            continue
        fix = reps[0]
        corrected = chunk[:o] + fix + chunk[o + ln:]
        sev = 'متوسط' if cat in ('TYPOS', 'GRAMMAR', 'CONFUSED_WORDS') else 'منخفض'
        res.append({'page': page, 'type': 'إملائي' if cat in ('TYPOS', 'CASING', 'PUNCTUATION', 'TYPOGRAPHY') else 'قواعد',
                    'lang': 'en' if lang.startswith('en') else 'ar', 'location': '',
                    'original': chunk, 'corrected': corrected,
                    'wrong': [bad], 'fixed': [fix] if fix.strip() else [],
                    'note': CAT_AR.get(cat, 'ملاحظة') + (f' — {m.get("message", "")}' if cat in ('GRAMMAR', 'CONFUSED_WORDS', 'MISC') or not lang.startswith('en') else ''),
                    'ref': '', 'severity': sev, 'confirm': not lang.startswith('en')})
    return res


# ---------------------------------------------------------------- report
UNIT_TITLES = {}


def build_report(items, meta, out_dir, cfg, log):
    os.makedirs(out_dir, exist_ok=True)
    order = {'مطابقة الكتاب': 0, 'قواعد': 1, 'إملائي': 2}
    items.sort(key=lambda x: (x['page'], order.get(x['type'], 3)))

    def hl(text, words, cls):
        s = html.escape(text)
        for w in sorted({w for w in words if w}, key=len, reverse=True):
            ew = html.escape(w)
            if ew in s:
                s = s.replace(ew, f'\x00{cls}\x01{ew}\x02', 1)
        return s.replace('\x00', '<span class="').replace('\x01', '">').replace('\x02', '</span>')

    rows, cur = [], None
    for n, it in enumerate(items, 1):
        if it.get('unit') != cur:
            cur = it.get('unit')
            rows.append(f'<tr class="unit"><td colspan="6">الوحدة {cur or "—"} — Unit {cur or "—"}</td></tr>')
        d = 'ltr' if it.get('lang') == 'en' else 'rtl'
        cls = {'مطابقة الكتاب': 'bk', 'قواعد': 'gr', 'إملائي': 'sp'}.get(it['type'], 'sp')
        conf = '<br><span class="cf">يحتاج تأكيد</span>' if it.get('confirm') else ''
        rows.append(f'''<tr><td class="c">{n}</td>
<td class="c"><b>{it["page"]}</b><br><small>درس {it.get("lesson") or "—"}</small></td>
<td class="c"><span class="tag {cls}">{it["type"]}</span><br><small class="sev">{it.get("severity","")}</small>{conf}</td>
<td><div dir="{d}" class="sent">{hl(it["original"], it.get("wrong", []), "bad")}</div></td>
<td><div dir="{d}" class="sent">{hl(it["corrected"], it.get("fixed", []), "good")}</div></td>
<td class="note">{html.escape(it.get("note",""))}{('<div class=ref>📖 '+html.escape(it["ref"])+'</div>') if it.get("ref") else ''}</td></tr>''')

    cnt = Counter(i['type'] for i in items)
    logo_tag, logo = '', cfg.get('logo')
    if logo and os.path.exists(logo):
        mime = 'image/png' if logo.lower().endswith('.png') else 'image/jpeg'
        b64 = base64.b64encode(open(logo, 'rb').read()).decode()
        logo_tag = f'<img class="wm" src="data:{mime};base64,{b64}">'
    doc = f'''<!DOCTYPE html><html lang="ar" dir="rtl"><head><meta charset="utf-8"><title>تقرير تدقيق</title>
<style>
@page {{ size: A4 landscape; margin: 12mm 10mm; }}
body {{ font-family: "Segoe UI", Tahoma, "DejaVu Sans", sans-serif; font-size: 10.5pt; color:#222; }}
.wm {{ position:fixed; top:50%; left:50%; width:55%; transform:translate(-50%,-50%); opacity:.07; z-index:-1 }}
h1 {{ text-align:center; margin:0 0 4px; font-size:18pt; color:#4a2c82 }}
.sub {{ text-align:center; color:#555; margin-bottom:8px }}
.sum {{ display:flex; gap:10px; justify-content:center; margin:8px 0 6px; flex-wrap:wrap }}
.sum div {{ border:1px solid #ccc; border-radius:6px; padding:5px 14px; background:#fafafa }}
.warn {{ text-align:center; color:#8a5a00; font-size:9pt; margin-bottom:10px }}
table {{ width:100%; border-collapse:collapse }} thead {{ display:table-header-group }} tr {{ page-break-inside:avoid }}
th {{ background:#4a2c82; color:#fff; padding:5px; font-size:10pt }}
td {{ border:1px solid #d5d5d5; padding:5px; vertical-align:top; background:rgba(255,255,255,.85) }}
td.c {{ text-align:center; white-space:nowrap }}
tr.unit td {{ background:#ece6f7; font-weight:bold; font-size:11.5pt; color:#4a2c82 }}
.sent {{ line-height:1.7 }} .sent[dir=ltr] {{ text-align:left }}
.bad {{ background:#ffd2d2; color:#b00000; font-weight:bold; padding:0 2px; border-radius:3px; text-decoration:line-through }}
.good {{ background:#d4f5d4; color:#06690f; font-weight:bold; padding:0 2px; border-radius:3px }}
.note {{ font-size:9.5pt; width:24% }} .ref {{ color:#4a2c82; font-size:8.5pt; margin-top:3px }}
.tag {{ padding:1px 6px; border-radius:4px; font-size:9pt; color:#fff }}
.tag.bk {{ background:#d9822b }} .tag.gr {{ background:#8e44ad }} .tag.sp {{ background:#2b7bd9 }}
.sev {{ color:#888 }} .cf {{ background:#fff3cd; color:#8a5a00; font-size:8pt; padding:0 4px; border-radius:3px }}
</style></head><body>{logo_tag}
<h1>تقرير تدقيق أوفلاين — {html.escape(meta["title"])}</h1>
<div class="sub">{html.escape(meta["sub"])}</div>
<div class="sum"><div>عدد الملاحظات: <b>{len(items)}</b></div>
<div>مطابقة الكتاب: <b>{cnt.get("مطابقة الكتاب",0)}</b></div><div>قواعد: <b>{cnt.get("قواعد",0)}</b></div>
<div>إملائي: <b>{cnt.get("إملائي",0)}</b></div></div>
<div class="warn">تدقيق آلي أولي (بدون ذكاء اصطناعي): الملاحظات المعلّمة «يحتاج تأكيد» راجعها على الصفحة. التدقيق العلمي (الأجوبة، أرقام الصفحات، الترجمة) غير مشمول.</div>
<table><thead><tr><th>#</th><th>الصفحة</th><th>النوع</th><th>النص في الملزمة</th><th>التصحيح (الجملة كاملة)</th><th>الملاحظة</th></tr></thead>
<tbody>{"".join(rows)}</tbody></table></body></html>'''
    base = os.path.join(out_dir, meta['file_base'])
    with open(base + '.html', 'w', encoding='utf-8') as f:
        f.write(doc)
    with open(base + '.json', 'w', encoding='utf-8') as f:
        json.dump(items, f, ensure_ascii=False, indent=1)
    pdf = html_to_pdf(base + '.html', base + '.pdf', cfg, log)
    return pdf or base + '.html'


def find_browser(cfg):
    cands = [cfg.get('browser'),
             r'C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe',
             r'C:\Program Files\Microsoft\Edge\Application\msedge.exe',
             r'C:\Program Files\Google\Chrome\Application\chrome.exe',
             r'C:\Program Files (x86)\Google\Chrome\Application\chrome.exe',
             shutil.which('msedge'), shutil.which('google-chrome'), shutil.which('chromium')]
    for c in cands:
        if c and os.path.exists(c):
            return c
    return None


def html_to_pdf(src, dst, cfg, log):
    b = find_browser(cfg)
    if not b:
        log('! Edge/Chrome not found - report saved as HTML only.')
        return None
    url = 'file:///' + os.path.abspath(src).replace('\\', '/')
    prof = tempfile.mkdtemp()
    try:
        subprocess.run([b, '--headless=new', '--disable-gpu', '--no-sandbox', '--no-pdf-header-footer',
                        f'--user-data-dir={prof}', f'--print-to-pdf={dst}', url],
                       capture_output=True, timeout=180)
    except Exception as e:
        log(f'! PDF conversion failed: {e}')
    shutil.rmtree(prof, ignore_errors=True)
    return dst if os.path.exists(dst) and os.path.getsize(dst) > 1000 else None


# ---------------------------------------------------------------- main pipeline
def run(malzama, books, out_dir, cfg, log=log_default, progress=None):
    t0 = time.time()
    log(f'Mudaqqiq v{VERSION}')
    log(f'Booklet: {os.path.basename(malzama)}')
    pdf = PDF(malzama)
    texts = [normalize(pdf.text(i)) for i in range(1, pdf.pages + 1)]
    if sum(len(t) for t in texts) < 200 * pdf.pages // 10:
        log('! The booklet itself looks scanned (no text layer) - results will be limited.')
    struct = detect_structure(texts)
    log(f'   {pdf.pages} pages')

    # الكتب
    index = None
    if books:
        bk = []
        for b in books:
            log(f'Book: {os.path.basename(b)}')
            bk.append((os.path.splitext(os.path.basename(b))[0], ocr_book(b, cfg, log)))
        index = BookIndex(bk)
        log(f'   {len(index.sents)} sentences indexed from books')

    # تجميع الجمل
    en_by_page, ar_by_page, sentences = {}, {}, []
    for p, t in enumerate(texts, 1):
        en, ar = [], []
        for line in t.split('\n'):
            if not line.strip():
                continue
            en += english_chunks(line)
            if is_arabic_line(line):
                ar += arabic_chunks(line)
        en_by_page[p], ar_by_page[p] = en, ar
        for c in en:
            for s in re.split(r'(?<=[\.\?!])\s+', c):
                if len(toks(s)) >= 4:
                    sentences.append((p, s.strip()))

    items = []
    if index:
        log('Comparing booklet text with the books...')
        items += check_against_book(sentences, index, log)
        items += check_ngrams(en_by_page, index)

    lt = LanguageTool(cfg, log)
    t1 = time.time()
    if lt.start():
        log(f'Spell/grammar check (LanguageTool ready in {int(time.time()-t1)}s)...')
        allowed = {w.lower() for w in cfg.get('extra_words', [])}
        if index:
            allowed |= {w for w, c in index.vocab.items() if c >= 2}
        from concurrent.futures import ThreadPoolExecutor
        langs = [cfg.get('english_variant', 'en-GB')] + (['ar'] if cfg.get('check_arabic') else [])
        jobs = []
        for lang in langs:
            src = en_by_page if lang.startswith('en') else ar_by_page
            batch, size = [], 0
            for p in range(1, pdf.pages + 1):
                batch.append((p, src[p])); size += sum(len(c) for c in src[p])
                if size > 15000 or p == pdf.pages:
                    jobs.append((lang, batch)); batch, size = [], 0
        allowed_ar = set()
        with ThreadPoolExecutor(max_workers=3) as ex:
            futs = [ex.submit(lt_findings, lt, b, lang, cfg, allowed if lang.startswith('en') else allowed_ar)
                    for lang, b in jobs]
            for k, fu in enumerate(futs, 1):
                items += fu.result()
                if progress:
                    progress(k, len(futs))
                log(f'   check {k}/{len(futs)}')
        lt.stop()

    # إزالة التكرار
    seen, final = set(), []
    for it in items:
        k = (it['page'], it['original'], tuple(it['wrong']))
        if k in seen:
            continue
        seen.add(k)
        it['unit'], it['lesson'] = struct.get(it['page'], (0, 0))
        final.append(it)

    name = os.path.splitext(os.path.basename(malzama))[0]
    meta = {'title': name,
            'sub': f'المرجع: {", ".join(os.path.basename(b) for b in books) or "بدون كتاب"} · {datetime.now():%Y-%m-%d %H:%M}',
            'file_base': f'تقرير_تدقيق_{name}'}
    out = build_report(final, meta, out_dir, cfg, log)
    log(f'DONE: {len(final)} notes in {int(time.time()-t0)}s\n{out}')
    return out, final


# ---------------------------------------------------------------- updater
UPDATE_BASE_DEFAULT = 'https://raw.githubusercontent.com/alialtabaay/mudaqqiq/main/'


def _ver(v):
    return tuple(int(x) for x in re.findall(r'\d+', str(v)))


def check_update(cfg):
    """يرجّع معلومات التحديث إذا أكو نسخة أحدث على GitHub، وإلا None."""
    import requests
    base = cfg.get('update_url') or UPDATE_BASE_DEFAULT
    r = requests.get(base + 'version.json', timeout=10, headers={'Cache-Control': 'no-cache'})
    r.raise_for_status()
    info = r.json()
    return info if _ver(info.get('version', '0')) > _ver(VERSION) else None


def apply_update(cfg, info, log):
    """ينزّل الملفات الجديدة، يتأكد منها، ياخذ نسخة احتياطية، ويبدّل."""
    import requests
    base = cfg.get('update_url') or UPDATE_BASE_DEFAULT
    tmp = tempfile.mkdtemp()
    files = info.get('files') or ['mudaqqiq.py']
    for name in files:
        r = requests.get(base + name, timeout=60)
        r.raise_for_status()
        dst = os.path.join(tmp, name)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        with open(dst, 'wb') as f:
            f.write(r.content)
        if name.endswith('.py'):
            compile(r.content.decode('utf-8'), name, 'exec')  # ما نبدّل بملف خربان
    bak = os.path.join(APP_DIR, 'backup', VERSION)
    os.makedirs(bak, exist_ok=True)
    for name in files:
        old = os.path.join(APP_DIR, name)
        if os.path.exists(old):
            shutil.copy2(old, os.path.join(bak, os.path.basename(name)))
        os.makedirs(os.path.dirname(old) or APP_DIR, exist_ok=True)
        shutil.copy2(os.path.join(tmp, name), old)
    shutil.rmtree(tmp, ignore_errors=True)
    log(f'Updated to v{info.get("version")} (backup in backup\\{VERSION})')


# ---------------------------------------------------------------- GUI
BRAND = '#4a2c82'; BRAND_DARK = '#3a2268'; BRAND_LIGHT = '#efe9f8'
BG = '#f6f5fa'; CARD = '#ffffff'; TEXT = '#22202a'; MUTED = '#6b6880'; OK = '#1e8e3e'


def gui():
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk
    cfg = load_config()
    root = tk.Tk()
    root.title(f'مُدقِّق الطابعي  v{VERSION}')
    root.geometry('980x720'); root.minsize(860, 640)
    root.configure(bg=BG)
    try:
        ico = os.path.join(APP_DIR, 'icon.ico')
        if os.name == 'nt' and os.path.exists(ico):
            root.iconbitmap(ico)
        icp = os.path.join(APP_DIR, 'icon.png')
        if os.path.exists(icp):
            root.iconphoto(True, tk.PhotoImage(file=icp))
    except Exception:
        pass

    F = 'Segoe UI' if os.name == 'nt' else 'DejaVu Sans'
    st = ttk.Style(root)
    st.theme_use('clam')
    st.configure('.', background=BG, foreground=TEXT, font=(F, 10))
    st.configure('Card.TFrame', background=CARD)
    st.configure('Card.TLabel', background=CARD, foreground=TEXT, font=(F, 10))
    st.configure('Title.TLabel', background=CARD, foreground=BRAND, font=(F, 12, 'bold'))
    st.configure('Muted.TLabel', background=CARD, foreground=MUTED, font=(F, 9))
    st.configure('Status.TLabel', background=BG, foreground=MUTED, font=(F, 9))
    st.configure('TEntry', fieldbackground='#fbfbfe', bordercolor='#d9d6e6', lightcolor='#d9d6e6', padding=6)
    st.configure('TButton', padding=(12, 6), font=(F, 10), background='#ece9f5', bordercolor='#d9d6e6')
    st.map('TButton', background=[('active', '#e1dcf0')])
    st.configure('Primary.TButton', padding=(26, 10), font=(F, 12, 'bold'), foreground='white',
                 background=BRAND, bordercolor=BRAND, focuscolor=BRAND)
    st.map('Primary.TButton', background=[('disabled', '#9b8fbd'), ('active', BRAND_DARK)])
    st.configure('Header.TButton', padding=(12, 5), font=(F, 9, 'bold'), foreground=BRAND,
                 background='white', bordercolor='white')
    st.map('Header.TButton', background=[('active', BRAND_LIGHT)])
    st.configure('TCheckbutton', background=CARD, font=(F, 10))
    st.map('TCheckbutton', background=[('active', CARD)])
    st.configure('Brand.Horizontal.TProgressbar', troughcolor='#e6e2f1', background=BRAND,
                 bordercolor='#e6e2f1', lightcolor=BRAND, darkcolor=BRAND, thickness=10)

    # ---------- header
    header = tk.Frame(root, bg=BRAND, height=78); header.pack(fill='x'); header.pack_propagate(False)
    try:
        logo_img = tk.PhotoImage(file=os.path.join(APP_DIR, 'logo_white.png'))
        tk.Label(header, image=logo_img, bg=BRAND).pack(side='right', padx=(10, 22))
        header.logo = logo_img
    except Exception:
        pass
    tbox = tk.Frame(header, bg=BRAND); tbox.pack(side='right', pady=12)
    tk.Label(tbox, text='مُدقِّق الملازم', bg=BRAND, fg='white', font=(F, 16, 'bold')).pack(anchor='e')
    tk.Label(tbox, text='تدقيق إملائي ومطابقة ويّا الكتاب — بدون إنترنت', bg=BRAND, fg='#d8cff0',
             font=(F, 9)).pack(anchor='e')
    hl = tk.Frame(header, bg=BRAND); hl.pack(side='left', padx=18)
    upd_btn = ttk.Button(hl, text='⟳  تحديث البرنامج', style='Header.TButton')
    upd_btn.pack(side='left')
    ver_lbl = tk.Label(hl, text=f'v{VERSION}', bg=BRAND, fg='#d8cff0', font=(F, 9)); ver_lbl.pack(side='left', padx=10)

    body = tk.Frame(root, bg=BG); body.pack(fill='both', expand=True, padx=20, pady=16)

    def card(parent, title, subtitle=''):
        outer = tk.Frame(parent, bg='#e4e0ef'); outer.pack(fill='x', pady=(0, 14))
        c = ttk.Frame(outer, style='Card.TFrame', padding=(18, 12)); c.pack(fill='both', padx=1, pady=1)
        ttk.Label(c, text=title, style='Title.TLabel').grid(row=0, column=0, columnspan=3, sticky='e')
        if subtitle:
            ttk.Label(c, text=subtitle, style='Muted.TLabel').grid(row=1, column=0, columnspan=3, sticky='e', pady=(0, 6))
        c.columnconfigure(1, weight=1)
        return c

    v_m = tk.StringVar()
    v_l = tk.StringVar(value=cfg.get('logo') or (os.path.join(APP_DIR, 'logo.png') if os.path.exists(os.path.join(APP_DIR, 'logo.png')) else ''))
    v_o = tk.StringVar(value=cfg.get('out_dir') or os.path.join(os.path.expanduser('~'), 'Documents', 'تقارير التدقيق'))
    v_ar = tk.BooleanVar(value=False)
    books = list(cfg.get('last_books', []))
    books = [b for b in books if os.path.exists(b)]

    c1 = card(body, 'الملفات', 'اختار الملزمة والكتب الرسمية للمادة نفسها')

    def row(c, r, label, var, cmd):
        ttk.Label(c, text=label, style='Card.TLabel').grid(row=r, column=2, sticky='e', padx=(10, 0), pady=5)
        ttk.Entry(c, textvariable=var).grid(row=r, column=1, sticky='ew', pady=5)
        ttk.Button(c, text='اختيار…', command=cmd).grid(row=r, column=0, padx=(0, 10), pady=5)

    row(c1, 2, 'الملزمة (PDF)', v_m, lambda: v_m.set(filedialog.askopenfilename(filetypes=[('PDF', '*.pdf')]) or v_m.get()))
    ttk.Label(c1, text='الكتب الرسمية', style='Card.TLabel').grid(row=3, column=2, sticky='ne', padx=(10, 0), pady=5)
    lb = tk.Listbox(c1, height=3, bd=1, relief='solid', highlightthickness=0, font=(F, 10),
                    selectbackground=BRAND_LIGHT, selectforeground=TEXT, justify='right')
    lb.grid(row=3, column=1, sticky='ew', pady=5)
    for b in books:
        lb.insert('end', os.path.basename(b))
    bf = ttk.Frame(c1, style='Card.TFrame'); bf.grid(row=3, column=0, padx=(0, 10), sticky='n')

    def add_book():
        for f in filedialog.askopenfilenames(filetypes=[('PDF', '*.pdf')]):
            if f not in books:
                books.append(f); lb.insert('end', os.path.basename(f))

    def clear_books():
        books.clear(); lb.delete(0, 'end')
    ttk.Button(bf, text='إضافة…', command=add_book).pack(fill='x', pady=(5, 3))
    ttk.Button(bf, text='مسح', command=clear_books).pack(fill='x')

    c2 = card(body, 'التقرير')
    row(c2, 2, 'اللوگو', v_l, lambda: v_l.set(filedialog.askopenfilename(filetypes=[('صور', '*.png *.jpg *.jpeg')]) or v_l.get()))
    row(c2, 3, 'حفظ في', v_o, lambda: v_o.set(filedialog.askdirectory() or v_o.get()))
    ttk.Checkbutton(c2, text='تدقيق العربي (تجريبي — غير دقيق)', variable=v_ar).grid(row=4, column=1, sticky='e', pady=(6, 0))

    act = tk.Frame(body, bg=BG); act.pack(fill='x', pady=(2, 8))
    btn = ttk.Button(act, text='▶  ابدأ التدقيق', style='Primary.TButton'); btn.pack(side='right')
    status = ttk.Label(act, text='جاهز', style='Status.TLabel'); status.pack(side='right', padx=16)
    pb = ttk.Progressbar(body, style='Brand.Horizontal.TProgressbar', mode='determinate')
    pb.pack(fill='x', pady=(0, 10))

    logf = tk.Frame(body, bg='#1f1b2b'); logf.pack(fill='both', expand=True)
    txt = tk.Text(logf, height=9, bg='#1f1b2b', fg='#d6d2e6', insertbackground='white', bd=0,
                  font=('Consolas' if os.name == 'nt' else 'DejaVu Sans Mono', 9), padx=12, pady=8, wrap='word')
    sb = ttk.Scrollbar(logf, command=txt.yview); txt.configure(yscrollcommand=sb.set)
    sb.pack(side='right', fill='y'); txt.pack(side='left', fill='both', expand=True)
    txt.tag_configure('err', foreground='#ff8a80'); txt.tag_configure('ok', foreground='#8be28b')

    foot = tk.Label(root, text='© مكتب الطابعي', bg=BG, fg=MUTED, font=(F, 8)); foot.pack(side='bottom', pady=(0, 6))

    log_path = os.path.join(APP_DIR, 'last_run.log')

    def log(m):
        try:
            with open(log_path, 'a', encoding='utf-8') as f:
                f.write(m + '\n')
        except Exception:
            pass
        tag = 'err' if m.startswith(('ERROR', '!', '   !')) else ('ok' if m.startswith('DONE') else '')
        root.after(0, lambda: (txt.insert('end', m + '\n', tag), txt.see('end')))

    def set_status(t):
        root.after(0, lambda: status.configure(text=t))

    def prog(i, n):
        root.after(0, lambda: pb.configure(maximum=max(n, 1), value=i))

    def save_cfg():
        try:
            c = json.load(open(CONFIG_PATH, encoding='utf-8')) if os.path.exists(CONFIG_PATH) else {}
            c.update({'logo': v_l.get(), 'out_dir': v_o.get(), 'last_books': list(books)})
            with open(CONFIG_PATH, 'w', encoding='utf-8') as f:
                json.dump(c, f, ensure_ascii=False, indent=1)
        except Exception:
            pass

    def start():
        if not v_m.get() or not os.path.exists(v_m.get()):
            messagebox.showerror('تنبيه', 'اختار ملف الملزمة أول'); return
        cfg['logo'] = v_l.get(); cfg['check_arabic'] = v_ar.get()
        save_cfg()
        try:
            open(log_path, 'w', encoding='utf-8').write(f'Mudaqqiq {VERSION}  {datetime.now()}\n')
        except Exception:
            pass
        txt.delete('1.0', 'end')
        btn.state(['disabled']); set_status('جاري التدقيق…')

        def work():
            try:
                out, items = run(v_m.get(), list(books), v_o.get(), cfg, log, prog)
                set_status(f'خلص ✓ — {len(items)} ملاحظة')
                root.after(0, lambda: os.startfile(out) if hasattr(os, 'startfile') else None)
            except Exception:
                import traceback
                log('ERROR:\n' + traceback.format_exc()); set_status('صار خطأ — شوف السجل')
            finally:
                root.after(0, lambda: btn.state(['!disabled']))
        threading.Thread(target=work, daemon=True).start()
    btn.configure(command=start)

    # ---------- updates
    def do_update(info):
        if not messagebox.askyesno('تحديث متوفر',
                                   f'أكو نسخة جديدة v{info.get("version")}\n\n{info.get("notes", "")}\n\nتريد تحدّث هسه؟'):
            return
        upd_btn.state(['disabled'])

        def w():
            try:
                apply_update(cfg, info, log)
                root.after(0, lambda: (messagebox.showinfo('تم التحديث', 'البرنامج راح ينفتح من جديد'), restart()))
            except Exception as e:
                log(f'! Update failed: {e}')
                root.after(0, lambda: (messagebox.showerror('فشل التحديث', str(e)), upd_btn.state(['!disabled'])))
        threading.Thread(target=w, daemon=True).start()

    def restart():
        try:
            subprocess.Popen([sys.executable, os.path.abspath(__file__)], cwd=APP_DIR)
        finally:
            root.destroy()

    def check(manual):
        def w():
            try:
                info = check_update(cfg)
            except Exception as e:
                if manual:
                    root.after(0, lambda: messagebox.showwarning('التحديث', f'ما گدرت أتصل بالسيرفر\n(يحتاج إنترنت)\n\n{e}'))
                return
            if info:
                root.after(0, lambda: (upd_btn.configure(text=f'⬆  تحديث متوفر v{info.get("version")}'),
                                       do_update(info) if manual else None))
            elif manual:
                root.after(0, lambda: messagebox.showinfo('التحديث', f'عندك آخر نسخة (v{VERSION}) ✓'))
        threading.Thread(target=w, daemon=True).start()

    upd_btn.configure(command=lambda: check(True))
    root.after(1500, lambda: check(False))  # فحص صامت عند الفتح
    root.mainloop()


def main():
    if len(sys.argv) == 1:
        return gui()
    ap = argparse.ArgumentParser(description='مُدقِّق الطابعي — تدقيق أوفلاين')
    ap.add_argument('--malzama', required=True)
    ap.add_argument('--book', action='append', default=[])
    ap.add_argument('--logo')
    ap.add_argument('--out', default='.')
    ap.add_argument('--no-arabic', action='store_true')
    a = ap.parse_args()
    cfg = load_config()
    if a.logo:
        cfg['logo'] = a.logo
    if a.no_arabic:
        cfg['check_arabic'] = False
    run(a.malzama, a.book, a.out, cfg)


if __name__ == '__main__':
    import multiprocessing
    multiprocessing.freeze_support()
    main()
