"""
═══════════════════════════════════════════════════════════════════════════
  スクールレーダー 熊本市の校区データ作成ツール（make_kumamoto.py）
═══════════════════════════════════════════════════════════════════════════
  熊本市の「熊本市立小中学校 索引簿」（住所→指定校のPDF）から data/kumamoto_city.json を作る。
  国の校区データ（国土数値情報）には熊本市の校区が入っていないため、これで補う。
  ※アプリの動作には不要。索引簿が更新されたときに作り直す。

  ■ 用意するもの
    熊本市「通学区域について」https://www.city.kumamoto.jp/kiji00361022/index.html
    の「住所から校区を検索」にある五十音別のPDF 8つ（あ行〜や・ら・わ行）

  ■ 使い方（AIに頼むときは、このファイルとPDFを渡して「熊本市のデータを作って」と伝える）
    pip install pdfplumber
    python3 tools/make_kumamoto.py PDFのフォルダ data/kumamoto_city.json 2026年5月版

  ■ できるファイルの形
    { "src": 出典, "ver": 版, "t": { "区|町名": [行, 行, ...] }, "list": [表示用の町, ...] }
      list（アプリの「校区を調べる」の表で使う。索引簿と同じ並び・同じ書き方）
        = { "g": 五十音の行(あ/か/さ/た/な/は/ま/や), "w": 区, "n": 町名(例 荒尾1～3丁目), "k": 読み仮名,
            "r": [ { "b": 街区の文字, "c": 地番の文字, "e": 指定小, "j": 指定中,
                     "z": [ { "b", "c", "e", "j" }, ... ] }, ... ] }
      町名は「麻生田2丁目」「秋津町秋田」のように丁目まで（「1～3丁目」は1つずつに分ける）
      行 = { "b": 街区の範囲, "c": 地番の範囲, "e": 指定小, "j": 指定中,
             "z": [ { "b", "c", "e", "j" }, ... ] ← 緩衝地区（ほかの学校も選べる範囲。無ければ省略）
                                                     e か j が空なら、その学校種はほかを選べない }
      範囲 = "A"（全域） / "R"（上記以外の全域） / [[番1,号1,番2,号2], ...]（号0=最初、99999=最後）
═══════════════════════════════════════════════════════════════════════════
"""
import sys, re, json, glob, unicodedata
import pdfplumber

END = 99999
WARD_RE = re.compile(r'^(中央区|東区|西区|南区|北区)$')
KANA_RE = re.compile(r'^[゠-ヿー・ ]+$')  # カタカナの読み仮名（空白入りもある）


def nfkc(s):
    return unicodedata.normalize('NFKC', s or '')


def norm_town(s):
    """町名を比べやすくする（全角→半角・空白除去・ヶ/ケ/ヵをそろえる・大字を取る）"""
    s = nfkc(s).replace(' ', '').replace('　', '')
    s = re.sub('[ヶケヵ]', 'ケ', s)
    return s.replace('大字', '')


def split_town_cell(text):
    """町名セル（例 '北区 アソウ ダ\\n麻生田2丁目'、'花立 3丁目\\n東区'）→ (区, 漢字の町名, 読み仮名)"""
    ward, parts, kana = '', [], ''
    for line in nfkc(text).split('\n'):
        line = line.strip()
        if not line or line == '町名':
            continue
        toks = line.split(' ')
        if WARD_RE.match(toks[0]):          # 「北区 アソウ ダ」= 区＋読み仮名
            ward = toks[0]
            kana = kana or ''.join(toks[1:])
            continue
        if WARD_RE.match(line):             # 「東区」だけの行
            ward = line
            continue
        if KANA_RE.match(line):             # 読み仮名だけの行
            kana = kana or line.replace(' ', '')
            continue
        parts.append(line)
    parts = list(dict.fromkeys(parts))   # ページの切れ目で同じ町名が2回入ることがあるので1つに
    return ward, norm_town(''.join(parts)), kana


# 読み仮名の1文字目 → 五十音の行
GYO = [('アイウエオヴ', 'あ'), ('カキクケコガギグゲゴ', 'か'), ('サシスセソザジズゼゾ', 'さ'),
       ('タチツテトダヂヅデド', 'た'), ('ナニヌネノ', 'な'), ('ハヒフヘホバビブベボパピプペポ', 'は'),
       ('マミムメモ', 'ま'), ('ヤユヨラリルレロワヲン', 'や')]
# 読み仮名が取れないときの予備：PDFのファイル名（熊本市のサイトでの名前）→ 行
FILE_GYO = {'ozqjd2mj': 'あ', 'zugmdido': 'か', 'ege2o1xc': 'さ', 'ktaoe7x8': 'た',
            'd3lph01q': 'な', 'ldlqzapq': 'は', '82ext5kq': 'ま', '3epyvyo6': 'や'}


# PDFの崩れで読み仮名が取れない町の読み（見つかったら足す）
KANA_FIX = {'花立': 'ハナタテ', '武蔵ケ丘': 'ムサシガオカ'}


def gyo_of(kana, fname):
    if kana:
        for chars, g in GYO:
            if kana[0] in chars:
                return g
    for code, g in FILE_GYO.items():
        if code in fname:
            return g
    return ''


def txt(cell):
    """表示用に、範囲の文字を1行にまとめる（改行→「、」。「~」で終わる行は続けてつなぐ）"""
    out = ''
    for ln in nfkc(cell).replace('~', '～').split('\n'):
        ln = ln.strip().rstrip('、')
        if not ln:
            continue
        out += ln if (not out or out.endswith('～') or ln.startswith('～') or re.match(r'^\d+(-\d+)?\s*号$', ln)) else '、' + ln
    return out.replace(' 号', '号')


def expand_town(name):
    """「荒尾1～3丁目」「秋津2・3丁目」を1丁目ずつに分ける"""
    m = re.match(r'^(.*?)(\d+(?:[~～・]\d+)*)丁目$', name)
    if not m:
        return [name]
    base, nums = m.group(1), m.group(2)
    out = []
    for part in nums.split('・'):
        if re.search(r'[~～]', part):
            a, b = re.split(r'[~～]', part)
            out += [f'{base}{n}丁目' for n in range(int(a), int(b) + 1)]
        else:
            out.append(f'{base}{part}丁目')
    return out


def parse_ranges(text):
    """番地の範囲の文字 → "A" / "R" / [[番1,号1,番2,号2],...] / None（空）"""
    t = nfkc(text).replace('〜', '~').replace('～', '~').strip()
    if not t:
        return None
    # 改行は区切りとして扱う。ただし「~」で終わる行や「37号」だけの行は前の行の続き
    lines = []
    for ln in t.split('\n'):
        ln = ln.strip()
        if not ln:
            continue
        if lines and (lines[-1].endswith('~') or ln.startswith('~') or re.match(r'^\d+(-\d+)?\s*号$', ln)):
            lines[-1] += ln
        else:
            lines.append(ln)
    items = [x.strip() for ln in lines for x in re.split('[、,]', ln) if x.strip()]
    ranges, has_all, has_rest = [], False, False
    last_ban = None   # 「10番34号～35号、37号」の「37号」は直前の番（10番）の37号
    for it in items:
        it = it.replace(' ', '')
        m = re.match(r'^(\d+)号$', it)
        if m and last_ban is not None:
            g = int(m.group(1)); ranges.append([last_ban, g, last_ban, g]); continue
        if it in ('全域',) or it.startswith('※地番全域'):
            has_all = True; continue
        if re.match(r'^(上記以外の全域|上記を除く.*全域|下記を除く.*全域)$', it):
            has_rest = True; continue
        m = re.match(r'^(\d+)番の(上記以外の全域|全域)', it)     # 「57番の上記以外の全域」など
        if m:
            n = int(m.group(1)); ranges.append([n, 0, n, END])
            m2 = re.search(r'と(\d+)番の全域', it)
            if m2:
                n2 = int(m2.group(1)); ranges.append([n2, 0, n2, END])
            continue
        if it in ('無番地',) or it.endswith('を除く)'):
            continue
        it = it.replace('旧地番の', '')
        left, _, right = it.partition('~')
        lm = re.match(r'^(\d+)番(?:地)?(?:(\d+)号?)?', left)
        if not lm:
            print('  ※読めない範囲:', it, file=sys.stderr); continue
        a = int(lm.group(1)); x = int(lm.group(2)) if lm.group(2) else None
        last_ban = a
        if not right:
            ranges.append([a, x or 0, a, x if x is not None else END]); continue
        if right == '終号':
            ranges.append([a, x or 0, a, END]); continue
        rm = re.match(r'^(\d+)番(?:地)?(?:(\d+)号?)?', right)
        if rm:                                   # 「~12番」「~1番17号」「~898番地5」
            b = int(rm.group(1)); y = int(rm.group(2)) if rm.group(2) else END
            ranges.append([a, x or 0, b, y]); continue
        rm = re.match(r'^(\d+)', right)          # 「~39号」「~23」→ 同じ番の中
        if rm:
            ranges.append([a, x or 0, a, int(rm.group(1))]); continue
        print('  ※読めない範囲:', it, file=sys.stderr)
    if ranges:
        return ranges + (['R'] if has_rest else [])
    if has_rest:
        return 'R'
    if has_all:
        return 'A'
    return None


def school(s):
    return nfkc(s).replace(' ', '').replace('\n', '')


HEADER_LABELS = ('町名', '音順', '街区', '地番', '指定', '緩衝', '許可', '住居表示', '(')


def strip_header(cell):
    """見出し行のセルから、見出しの文字以外（ページの切れ目でくっついた値）を取り出す"""
    out = []
    for ln in nfkc(cell).split('\n'):
        if not ln.strip():
            continue
        if any(ln.replace(' ', '').startswith(h) for h in HEADER_LABELS):
            continue
        out.append(ln)
    return '\n'.join(out)


def main(pdf_dir, out_path, ver):
    towns = {}
    cur_ward, cur_town = '', ''
    pending = {}          # ページの切れ目で見出し行にくっついた値（次の行に渡す）
    prev_row = None
    disp, prev_disp, cur_kana = [], None, ''   # 表示用の一覧
    order = 'あかさたなはまや'
    files = sorted(glob.glob(pdf_dir.rstrip('/') + '/*.pdf'),
                   key=lambda f: order.index(gyo_of('', f)) if gyo_of('', f) else 99)
    for f in files:
        with pdfplumber.open(f) as pdf:
            for page in pdf.pages:
                for table in page.extract_tables():
                    for r in table:
                        r = [(c or '') for c in r]
                        if '街 区' in r[2] or '街 区' in r[6] or r[1] == '町名':
                            # 見出し行。くっついた値があれば次の行のために取っておく
                            for i in range(1, 10):
                                v = strip_header(r[i])
                                if v:
                                    pending[i] = v
                            continue
                        for i, v in list(pending.items()):
                            if i == 1 and r[1]:
                                r[1] = v + '\n' + r[1]
                            elif not r[i].strip():
                                r[i] = v
                        pending = {}
                        b, c = parse_ranges(r[2]), parse_ranges(r[3])
                        e, j = school(r[4]), school(r[5])
                        zb, zc = parse_ranges(r[6]), parse_ranges(r[7])
                        ze, zj = school(r[8]), school(r[9])
                        # 緩衝地区だけの行（指定校の欄が空）＝ 上の行の緩衝地区の続き
                        if not (b or c or e or j) and prev_row is not None:
                            if ze or zj:
                                last = prev_row['z'][-1] if prev_row.get('z') else None
                                lastd = prev_disp['z'][-1] if prev_disp.get('z') else None
                                zbt, zct = txt(r[6]), txt(r[7])
                                if not (zb or zc) and last:          # 範囲の欄が結合セル → 上と同じ範囲
                                    zb, zc = last.get('b'), last.get('c')
                                    zbt, zct = lastd.get('b', ''), lastd.get('c', '')
                                prev_row.setdefault('z', []).append(
                                    {k: v for k, v in {'b': zb, 'c': zc, 'e': ze, 'j': zj}.items() if v})
                                prev_disp.setdefault('z', []).append(
                                    {k: v for k, v in {'b': zbt, 'c': zct, 'e': ze, 'j': zj}.items() if v})
                            continue
                        new_town = False
                        if r[1].strip():                       # 新しい町
                            w, t, k = split_town_cell(r[1])
                            new_town = (w or cur_ward, t or cur_town) != (cur_ward, cur_town)
                            cur_ward = w or cur_ward
                            cur_town = t or cur_town
                            cur_kana = k or (cur_kana if not new_town else '')
                        row = {'b': b, 'c': c, 'e': e, 'j': j}
                        # 結合セル（上の行と同じ学校）で空欄になっている場合は、すぐ上の行から引き継ぐ
                        if prev_row is not None:
                            row['e'] = row['e'] or prev_row['e']
                            row['j'] = row['j'] or prev_row['j']
                        if (zb or zc) and (ze or zj):
                            row['z'] = [{k: v for k, v in {'b': zb, 'c': zc, 'e': ze, 'j': zj}.items() if v}]
                        row = {k: v for k, v in row.items() if v}
                        for name in expand_town(cur_town):
                            towns.setdefault(cur_ward + '|' + name, []).append(row)
                        prev_row = row
                        # 表示用（索引簿の書き方のまま）。ページをまたいだ同じ町は1つにまとめる
                        d = {'b': txt(r[2]), 'c': txt(r[3]), 'e': row.get('e', ''), 'j': row.get('j', '')}
                        if row.get('z'):
                            d['z'] = [{k: v for k, v in {'b': txt(r[6]), 'c': txt(r[7]), 'e': ze, 'j': zj}.items() if v}]
                        d = {k: v for k, v in d.items() if v}
                        if not disp or (disp[-1]['w'], disp[-1]['n']) != (cur_ward, cur_town):
                            disp.append({'g': gyo_of(cur_kana, f), 'w': cur_ward, 'n': cur_town, 'k': cur_kana, 'r': []})
                        elif cur_kana and not disp[-1]['k']:
                            disp[-1]['k'] = cur_kana
                        disp[-1]['r'].append(d)
                        prev_disp = d
    for x in disp:
        x['n'] = x['n'].replace('~', '～')
        if not x['k']:
            x['k'] = next((v for t, v in KANA_FIX.items() if x['n'].startswith(t)), '')
            if not x['k']:
                print('  ※読み仮名が無い町:', x['w'], x['n'], file=sys.stderr)
    data = {'src': '熊本市立小中学校 索引簿（熊本市教育委員会）', 'ver': ver, 't': towns, 'list': disp}
    with open(out_path, 'w', encoding='utf-8') as fp:
        json.dump(data, fp, ensure_ascii=False, separators=(',', ':'))
    rows = sum(len(v) for v in towns.values())
    print(f'町丁目 {len(towns)}件・行 {rows}件・表示用の町 {len(disp)}件 を書き出しました → {out_path}')


if __name__ == '__main__':
    main(sys.argv[1], sys.argv[2], sys.argv[3] if len(sys.argv) > 3 else '')
