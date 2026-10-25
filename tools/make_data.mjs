/*
═══════════════════════════════════════════════════════════════════════════
  スクールレーダー データ作成ツール（make_data.mjs）
═══════════════════════════════════════════════════════════════════════════
  国土数値情報（国土交通省）の熊本県データから、アプリが読む data/ の4ファイルを作る。
  ※このツールはアプリの動作には不要。データを作り直すとき（毎年の更新など）だけ使う。

  ■ 用意するもの（すべて無料・登録不要）
    https://nlftp.mlit.go.jp/ksj/ で「熊本」の最新年度のZIPをダウンロード
      ・A27 小学校区  … 例: A27-23_43_GML.zip
      ・A32 中学校区  … 例: A32-23_43_GML.zip
      ・P29 学校      … 例: P29-23_43_GML.zip

  ■ 使い方（AIに頼むときは、このファイルとZIPを渡して「データを作って」と伝える）
    npm i mapshaper@0.6
    node tools/make_data.mjs --a27 A27.zip --a32 A32.zip --p29 P29.zip --year 令和5年度 --out data

  ■ できるファイル
    data/schools.json  … 学校 [{n:名前, k:種類, y:緯度, x:経度, a:住所, o:国立/公立/私立}]
                          k: e=小 j=中 g=義務教育 m=中等教育 h=高校 t=高専 c=短大 u=大学 s=特別支援
    data/shogakku.json … 小学校区 GeoJSON（properties: n=学校名, c=市町村コード, y/x=学校の位置）
    data/chugakku.json … 中学校区 GeoJSON（同上）
    data/meta.json     … 出典・年度・作成日

  ■ しくみ
    1. ZIPを展開し、GeoJSON（なければShapefile）を探す
    2. mapshaper で GeoJSON にし、校区の線を約12mの精度で簡略化（隣の校区との境目はずれない）
    3. 項目名は年度で変わるので、中身（「〜小学校」で終わる値など）から自動で見分ける
    4. 校区と学校を名前で結びつけ、校区に学校の位置を入れておく
═══════════════════════════════════════════════════════════════════════════
*/
import { execFileSync } from 'node:child_process';
import fs from 'node:fs';
import path from 'node:path';
import os from 'node:os';

// ---- 引数を読む ----
const args = {};
for (let i = 2; i < process.argv.length; i += 2) args[process.argv[i].replace(/^--/, '')] = process.argv[i + 1];
const OUT = args.out || 'data';
const SIMPLIFY_M = Number(args.simplify || 12); // 校区の線を何mの精度にするか（小さいほど正確・重い。12mで2ファイル合計 約0.9MB(圧縮後)）
fs.mkdirSync(OUT, { recursive: true });
const MAPSHAPER = path.resolve(path.dirname(new URL(import.meta.url).pathname), '..', 'node_modules', '.bin', 'mapshaper');
const ms = fs.existsSync(MAPSHAPER) ? MAPSHAPER : 'mapshaper';

// ---- ZIPを展開して、地図データのファイル一覧を返す ----
function extract(zip){
  if (!zip) return [];
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'ksj-'));
  execFileSync('unzip', ['-o', '-q', zip, '-d', dir]);
  // ZIPの中にさらにZIPがある場合も展開する
  const walk = d => fs.readdirSync(d, { withFileTypes: true }).flatMap(e =>
    e.isDirectory() ? walk(path.join(d, e.name)) : [path.join(d, e.name)]);
  walk(dir).filter(f => f.toLowerCase().endsWith('.zip')).forEach(z =>
    execFileSync('unzip', ['-o', '-q', z, '-d', path.dirname(z)]));
  const files = walk(dir);
  const gj = files.filter(f => /\.geojson$/i.test(f));
  return gj.length ? gj : files.filter(f => /\.shp$/i.test(f));
}

// ---- 1ファイルをGeoJSONとして読む（必要なら簡略化） ----
function readGeo(file, simplify){
  const out = path.join(os.tmpdir(), 'ms-' + Date.now() + Math.random().toString(36).slice(2) + '.json');
  const inp = [file];
  // Shapefileで文字コード指定(.cpg)が無ければ Shift_JIS とみなす（国土数値情報の古い形式）
  if (/\.shp$/i.test(file) && !fs.existsSync(file.replace(/\.shp$/i, '.cpg'))) inp.push('encoding=shiftjis');
  const cmd = ['-i', ...inp];
  if (simplify) cmd.push('-simplify', 'interval=' + SIMPLIFY_M, 'keep-shapes');
  cmd.push('-o', 'format=geojson', 'precision=0.00001', out);
  execFileSync(ms, cmd, { stdio: ['ignore', 'ignore', 'inherit'] });
  const fc = JSON.parse(fs.readFileSync(out, 'utf8'));
  fs.unlinkSync(out);
  return fc;
}
const isPoly = fc => fc.features.some(f => f.geometry && /Polygon/.test(f.geometry.type));
const isPoint = fc => fc.features.some(f => f.geometry && /Point/.test(f.geometry.type));

// ---- 項目を中身から見分ける ----
// その項目の値のうち、条件に合うものの割合
function ratio(features, key, test){
  let ok = 0, n = 0;
  for (const f of features.slice(0, 400)){
    const v = f.properties[key];
    if (v == null || v === '') continue;
    n++; if (test(String(v))) ok++;
  }
  return n ? ok / n : 0;
}
function pickKey(features, test){
  const keys = Object.keys(features[0].properties);
  let best = null, bestR = 0;
  keys.forEach(k => { const r = ratio(features, k, test); if (r > bestR){ bestR = r; best = k; } });
  return bestR > 0.5 ? best : null;
}
const NAME_RE = /(学校|大学|学園|校)$/;
const ADDR_RE = /(熊本県|市|町|村).*\d|番地|丁目/;
const CODE_RE = /^43\d{3}$/;

// ---- 名前から種類を決める（アプリの kindFromName と同じ考え方） ----
function kindFromName(n){
  if (/幼稚園|保育|こども園|子ども園|専修|各種学校/.test(n)) return null;
  if (/支援学校|養護学校|盲学校|聾学校|ろう学校/.test(n)) return 's';
  if (/義務教育学校/.test(n)) return 'g';
  if (/中等教育学校/.test(n)) return 'm';
  if (/高等専門学校/.test(n)) return 't';
  if (/短期大学/.test(n)) return 'c';
  if (/大学/.test(n)) return 'u';
  if (/高等学校|高校/.test(n)) return 'h';
  if (/中学校/.test(n)) return 'j';
  if (/小学校/.test(n)) return 'e';
  return null;
}
// P29の学校分類コード → アプリの種類（幼稚園16011・こども園16013・専修16016・各種16015は使わない）
const P29_KIND = { '16001':'e', '16002':'j', '16003':'m', '16004':'h', '16005':'t',
                   '16006':'c', '16007':'u', '16012':'s', '16014':'g' };
// 学校名を比べやすくする（アプリの normName と同じ）
//   空白・「〇〇市立」「〇〇組合立」・先頭の「義務教育学校」・かっこ書きを取り、ヶ/ケ/ヵ をそろえる
function normName(s){
  return String(s || '').replace(/[\s　]/g, '')
    .replace(/^.*組合立/, '')
    .replace(/^(.{1,6}?[市町村]立|県立|国立|私立)/, '')
    .replace(/^義務教育学校/, '')
    .replace(/[（(].*?[）)]/g, '')
    .replace(/[ヶケヵが]/g, 'ケ');
}
function distM(a, b){
  const R = 6371000, r = x => x * Math.PI / 180;
  const s = Math.sin(r(b.lat - a.lat) / 2) ** 2 + Math.cos(r(a.lat)) * Math.cos(r(b.lat)) * Math.sin(r(b.lng - a.lng) / 2) ** 2;
  return 2 * R * Math.asin(Math.sqrt(s));
}
// 多角形のおおよその中心（学校の位置が見つからないときの目安）
function centroid(geom){
  const ring = geom.type === 'Polygon' ? geom.coordinates[0] : geom.coordinates[0][0];
  let x = 0, y = 0; ring.forEach(p => { x += p[0]; y += p[1]; });
  return { lat: y / ring.length, lng: x / ring.length };
}

// ---- 学校（点）を読む ----
function readSchools(files){
  const out = [];
  files.forEach(f => {
    const fc = readGeo(f, false);
    if (!isPoint(fc) || !fc.features.length) return;
    const feats = fc.features.filter(x => x.properties);
    // P29は名前の項目が決まっている（幼稚園なども混ざるので中身からは見分けにくい）
    const nk = feats[0].properties.P29_004 !== undefined ? 'P29_004' : pickKey(feats, v => NAME_RE.test(v));
    if (!nk) return;
    const ak = pickKey(feats, v => ADDR_RE.test(v) && !NAME_RE.test(v));
    const ck = pickKey(feats, v => CODE_RE.test(v));
    // P29（学校）の項目。2023年度版で中身を確認して決めたもの:
    //   P29_003 = 学校分類コード（下の P29_KIND）
    //   P29_006 = 管理者（1=国 2=都道府県 3=市町村 4=民間 0=その他）
    //   P29_007 = 休校区分（小・中学校で 1=通常 9=休校。それ以外の学校は 0）→ 9 は除く
    //   P29_009 = キャンパス名（大学など。名前の後ろに付ける）
    const isP29 = feats[0].properties.P29_003 !== undefined;
    console.log('  点データ', path.basename(f), '名前=', nk, '住所=', ak, '市町村=', ck, 'P29形式=', isP29);
    feats.forEach(x => {
      const p = x.properties;
      let n = String(p[nk] || '').trim();
      // 種類: P29は分類コードで決める（「産山学園」のように名前で分からない学校があるため）
      const k = isP29 ? P29_KIND[String(p.P29_003)] : kindFromName(n);
      if (!n || !k) return;
      const closed = isP29 && String(p.P29_007) === '9';          // 休校（一覧には出さず、校区との結びつけだけに使う）
      if (isP29 && p.P29_009) n += '（' + p.P29_009 + '）';       // キャンパス名
      const o = isP29 ? ({ '1':'国立', '2':'公立', '3':'公立', '4':'私立' }[String(p.P29_006)] || '') : '';
      const [lng, lat] = x.geometry.coordinates;
      out.push({ n, k, y: +lat.toFixed(6), x: +lng.toFixed(6), a: ak ? String(p[ak] || '').replace(/^熊本県/, '') : '', o, c: ck ? String(p[ck]) : '', closed });
    });
  });
  return out;
}

// ---- 校区（多角形）を読む ----
function readKu(files, schoolPts){
  let polys = null, pts = [];
  files.forEach(f => {
    const fc = readGeo(f, false);
    if (isPoly(fc)) polys = f;               // 多角形のファイル
    else if (isPoint(fc)) pts = pts.concat(readSchools([f])); // 校区データに入っている学校の点
  });
  if (!polys) throw new Error('校区の多角形が見つかりません');
  const fc = readGeo(polys, true);
  const feats = fc.features.filter(x => x.geometry);
  const nk = pickKey(feats, v => NAME_RE.test(v));
  const ck = pickKey(feats, v => CODE_RE.test(v));
  console.log('  校区', path.basename(polys), feats.length + '件', '名前=', nk, '市町村=', ck);
  const all = pts.concat(schoolPts);
  let matched = 0;
  const out = feats.map(x => {
    const p = x.properties;
    const n = String(p[nk] || '').trim();
    const c = ck ? String(p[ck]) : '';
    const props = { n, c };
    // 校区の学校の位置：同じ名前の学校のうち、校区の中心にいちばん近いもの
    const key = normName(n), mid = centroid(x.geometry);
    // まず同じ市町村の中で探し、無ければ市町村を問わず探す（市町村コードが食い違うデータがあるため）
    const find = sameMuni => {
      let best = null, bestD = Infinity;
      all.forEach(s => {
        if (normName(s.n) !== key) return;
        if (sameMuni && c && s.c && s.c !== c) return;
        const d = distM(mid, { lat: s.y, lng: s.x });
        if (d < bestD){ bestD = d; best = s; }
      });
      return best && bestD < 20000 ? best : null;
    };
    const best = find(true) || find(false);
    if (best){
      props.y = best.y; props.x = best.x; matched++;
      if (best.closed) props.closed = 1; // 休校中の学校
    }else{
      console.log('    学校の位置が見つからない校区:', n, c);
    }
    return { type: 'Feature', properties: props, geometry: x.geometry };
  });
  console.log('  学校の位置が分かった校区:', matched + '/' + out.length);
  return { type: 'FeatureCollection', features: out };
}

// ---- 実行 ----
console.log('学校（P29）を読み込み中…');
const schools = readSchools(extract(args.p29));
// 同じ学校の重複（同じ名前で100m以内）を1つにする
const uniq = [];
schools.forEach(s => {
  if (!uniq.some(u => u.n === s.n && distM({lat:u.y,lng:u.x}, {lat:s.y,lng:s.x}) < 100)) uniq.push(s);
});
console.log('  学校', uniq.length + '件');

let e = null, j = null;
if (args.a27){ console.log('小学校区（A27）を読み込み中…'); e = readKu(extract(args.a27), uniq); }
if (args.a32){ console.log('中学校区（A32）を読み込み中…'); j = readKu(extract(args.a32), uniq); }

const write = (name, obj) => {
  fs.writeFileSync(path.join(OUT, name), JSON.stringify(obj));
  console.log('書き出し', path.join(OUT, name), Math.round(fs.statSync(path.join(OUT, name)).size / 1024) + 'KB');
};
if (uniq.length) write('schools.json', uniq.filter(s => !s.closed).map(({ c, closed, ...rest }) => rest));
if (e) write('shogakku.json', e);
if (j) write('chugakku.json', j);
write('meta.json', {
  source: '国土数値情報（学校・小学校区・中学校区データ／国土交通省）',
  year: args.year || '',
  made: new Date().toISOString().slice(0, 10)
});
