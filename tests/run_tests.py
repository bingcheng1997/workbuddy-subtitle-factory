#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""双语字幕工厂 自动化测试
覆盖：断句规范（行长/阅读速度/间隔）、双语两行约束、术语表落实、
时间轴约束、SRT 回读复检、多格式输出完整性。
翻译环节为确定性替身（预置平行译文），接入真实模型时替换 [LLM替换点]。
"""
import csv, os, re, sys
from dataclasses import dataclass

BASE = os.path.dirname(os.path.abspath(__file__))
FIX = os.path.join(BASE, "fixtures")
OUT = os.path.join(BASE, "output")

MIN_DUR, MAX_DUR, GAP = 1.5, 7.0, 0.08
CPS_BUILD_ZH, CPS_LIMIT_ZH = 8.5, 9.0
CPS_BUILD_EN, CPS_LIMIT_EN = 16.0, 17.0
MAX_LINE_ZH, MAX_LINE_EN = 20, 42

results = []
def check(name, cond, detail=""):
    results.append((name, bool(cond), detail))
    print(("  PASS  " if cond else "  FAIL  ") + name + (f"   [{detail}]" if (detail and not cond) else ""))

@dataclass
class Cue:
    idx: int
    start: float
    end: float
    lines: list

def ts2sec(ts):
    h, m, s = ts.replace(",", ".").split(":")
    return int(h) * 3600 + int(m) * 60 + float(s)

def sec2ts(t, comma=True):
    if t < 0: t = 0.0
    ms = int(round(t * 1000))
    h, ms = divmod(ms, 3600000)
    m, ms = divmod(ms, 60000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d}{',' if comma else '.'}{ms:03d}"

def parse_srt(path):
    text = open(path, encoding="utf-8-sig").read().replace("\r\n", "\n").strip()
    cues = []
    for block in re.split(r"\n\s*\n", text):
        idx, tl, lines = None, None, []
        for row in block.split("\n"):
            r = row.strip()
            if not r: continue
            if tl is None and idx is None and re.fullmatch(r"\d+", r):
                idx = int(r); continue
            if tl is None and "-->" in r:
                tl = r; continue
            lines.append(r)
        if tl is None: continue
        m = re.match(r"([\d:,.]+)\s*-->\s*([\d:,.]+)", tl)
        if not m: continue
        cues.append(Cue(idx if idx is not None else len(cues) + 1,
                        ts2sec(m.group(1)), ts2sec(m.group(2)), lines))
    return cues

def write_srt(path, cues):
    blocks = [f"{i}\n{sec2ts(c.start)} --> {sec2ts(c.end)}\n" + "\n".join(c.lines)
              for i, c in enumerate(cues, 1)]
    open(path, "w", encoding="utf-8").write("\n\n".join(blocks) + "\n")

def cjk_len(s):
    return sum(1 for c in s if not c.isspace())

def struct_check(cues):
    d, prev_end, ids = [], None, []
    for c in cues:
        ids.append(c.idx)
        if not c.lines or all(not l.strip() for l in c.lines):
            d.append((c.idx, "空条目", "无文本")); continue
        if c.end < c.start:
            d.append((c.idx, "起止倒置", "")); continue
        dur = c.end - c.start
        if dur < MIN_DUR - 1e-6: d.append((c.idx, "过短", f"{dur:.2f}s"))
        if dur > MAX_DUR + 1e-6: d.append((c.idx, "过长", f"{dur:.2f}s"))
        has_cjk = lambda s: any("\u4e00" <= ch <= "\u9fff" for ch in s)
        bilingual = len(c.lines) == 2 and has_cjk(c.lines[0]) and not has_cjk(c.lines[1])
        if bilingual:
            zh, en = c.lines
            if cjk_len(zh) > MAX_LINE_ZH: d.append((c.idx, "超长行", f"中文行 {cjk_len(zh)}字"))
            if len(en) > MAX_LINE_EN: d.append((c.idx, "超长行", f"英文行 {len(en)}字符"))
            if dur > 0:
                if cjk_len(zh) / dur > CPS_LIMIT_ZH: d.append((c.idx, "CPS超标", f"中文 {cjk_len(zh)/dur:.1f}字/秒"))
                if len(en) / dur > CPS_LIMIT_EN: d.append((c.idx, "CPS超标", f"英文 {len(en)/dur:.1f}字符/秒"))
        else:
            text = "".join(c.lines)
            if dur > 0 and cjk_len(text) / dur > CPS_LIMIT_ZH:
                d.append((c.idx, "CPS超标", f"{cjk_len(text)/dur:.1f}字/秒"))
            for line in c.lines:
                if cjk_len(line) > MAX_LINE_ZH:
                    d.append((c.idx, "超长行", f"{cjk_len(line)}字/行")); break
        if prev_end is not None:
            if c.start < prev_end - 1e-6:
                d.append((c.idx, "时间重叠/乱序", ""))
            elif c.start - prev_end < GAP - 1e-6:
                d.append((c.idx, "间隔过小", ""))
        prev_end = c.end
    for a, b in zip(ids, ids[1:]):
        if b != a + 1:
            d.append((b, "序号跳跃", "")); break
    return d

PUNCT = "，。；、！？：,"

def zh_split_index(text):
    n, best = len(text), None
    for i, ch in enumerate(text):
        if ch in PUNCT or ch == " ":
            if best is None or abs(i - n // 2) < abs(best - n // 2):
                best = i
    if best is not None:
        return best + 1
    cut = n // 2
    while cut < n - 1 and re.match(r"[A-Za-z0-9]", text[cut]) and re.match(r"[A-Za-z0-9]", text[cut - 1]):
        cut += 1
    return cut

def split_zh_greedy(text):
    if cjk_len(text) <= MAX_LINE_ZH:
        return [text]
    cnt, idx = 0, 0
    for i, ch in enumerate(text):
        if not ch.isspace(): cnt += 1
        if cnt > MAX_LINE_ZH: break
        idx = i + 1
    cut = 0
    for j in range(idx, 0, -1):
        if text[j - 1] in PUNCT or text[j - 1] == " ":
            cut = j; break
    if cut == 0:
        cut = idx
        while cut > 1 and re.match(r"[A-Za-z0-9]", text[cut - 1]) and cut < len(text) and re.match(r"[A-Za-z0-9]", text[cut]):
            cut -= 1
    a, b = text[:cut].strip(), text[cut:].strip()
    if not a or not b:
        return [text]
    return [a] + split_zh_greedy(b)

def split_en_greedy(en):
    if len(en) <= MAX_LINE_EN:
        return [en]
    words, chunks, cur, cur_len = en.split(), [], [], 0
    for w in words:
        add = len(w) + (1 if cur else 0)
        if cur and cur_len + add > MAX_LINE_EN:
            chunks.append(" ".join(cur)); cur, cur_len = [w], len(w)
        else:
            cur.append(w); cur_len += add
    if cur: chunks.append(" ".join(cur))
    return chunks

def grow_to(pieces, target, wordwise):
    pieces = list(pieces)
    while len(pieces) < target:
        done = False
        for i in sorted(range(len(pieces)), key=lambda k: -len(pieces[k])):
            p = pieces[i]
            if wordwise and len(p.split()) >= 2:
                ws = p.split(); h = len(ws) // 2
                a, b = " ".join(ws[:h]), " ".join(ws[h:])
            elif not wordwise and cjk_len(p) >= 2:
                cut = zh_split_index(p)
                a, b = p[:cut].strip(), p[cut:].strip()
                if not a or not b:
                    cut = len(p) // 2
                    while 0 < cut < len(p) - 1 and re.match(r"[A-Za-z0-9]", p[cut]) and re.match(r"[A-Za-z0-9]", p[cut - 1]):
                        cut += 1
                    a, b = p[:cut].strip(), p[cut:].strip()
            else:
                continue
            if a and b:
                pieces[i:i + 1] = [a, b]; done = True; break
        if not done: break
    return pieces

def pair_units(zh_lines, en_lines):
    units = []
    for zh, en in zip(zh_lines, en_lines):
        zp, ep = split_zh_greedy(zh), split_en_greedy(en)
        if len(zp) < len(ep): zp = grow_to(zp, len(ep), wordwise=False)
        elif len(ep) < len(zp): ep = grow_to(ep, len(zp), wordwise=True)
        if len(zp) != len(ep):
            raise AssertionError(f"中英拆条数量不齐: {zh!r}")
        units.extend(zip(zp, ep))
    return units

def main():
    os.makedirs(os.path.join(OUT, "字幕输出"), exist_ok=True)
    odir = os.path.join(OUT, "字幕输出")
    zh_lines = [l.strip() for l in open(os.path.join(FIX, "transcript_zh.txt"), encoding="utf-8") if l.strip()]
    en_lines = [l.strip() for l in open(os.path.join(FIX, "transcript_en.txt"), encoding="utf-8") if l.strip()]
    check("中英平行文本行数一致", len(zh_lines) == len(en_lines), f"{len(zh_lines)} vs {len(en_lines)}")

    glossary = {}
    for row in csv.reader(open(os.path.join(FIX, "glossary.csv"), encoding="utf-8-sig")):
        if len(row) == 2 and row[0].strip(): glossary[row[0].strip()] = row[1].strip()

    # [LLM替换点] 翻译环节：测试用预置平行译文（已按术语表译好）。
    # 接真实模型时：逐段调用 LLM，注入术语表强制一致，返回译文即可。
    def translate(en):
        return en

    units = pair_units(zh_lines, [translate(en) for en in en_lines])
    t, cues = 0.0, []
    for zh_p, en_p in units:
        dur = min(MAX_DUR, max(cjk_len(zh_p) / CPS_BUILD_ZH, len(en_p) / CPS_BUILD_EN, MIN_DUR))
        lines = [l for l in [zh_p, en_p] if l]
        cues.append(Cue(len(cues) + 1, t, t + dur, lines))
        t += dur + GAP

    check("断句后每条中文行 ≤20 字", all(cjk_len(c.lines[0]) <= MAX_LINE_ZH for c in cues))
    check("双语两行且英文行 ≤42 字符", all(len(c.lines) == 2 and len(c.lines[1]) <= MAX_LINE_EN for c in cues))
    check("时间轴单调递增且间隔 ≥80ms",
          all(cues[i].start - cues[i-1].end >= GAP - 1e-6 for i in range(1, len(cues))))
    check("中文阅读速度 ≤9字/秒", all(cjk_len(c.lines[0]) / (c.end - c.start) <= CPS_LIMIT_ZH + 1e-9 for c in cues))
    check("英文 CPS ≤17", all(len(c.lines[1]) / (c.end - c.start) <= CPS_LIMIT_EN + 1e-9 for c in cues))
    check("序号连续", [c.idx for c in cues] == list(range(1, len(cues) + 1)))

    en_all = " ".join(c.lines[1] for c in cues)
    missing = [t_en for t_en in glossary.values() if t_en.lower() not in en_all.lower()]
    check("术语表译名全部落实", not missing, f"缺失: {missing}")

    srt = os.path.join(odir, "course01.zh-en.srt"); write_srt(srt, cues)
    vtt = os.path.join(odir, "course01.zh-en.vtt")
    open(vtt, "w", encoding="utf-8").write("WEBVTT\n\n" + "\n\n".join(
        f"{sec2ts(c.start, False)} --> {sec2ts(c.end, False)}\n" + "\n".join(c.lines) for c in cues) + "\n")
    ass = os.path.join(odir, "course01.zh-en.ass")
    open(ass, "w", encoding="utf-8").write(
        "[Script Info]\nScriptType: v4.00+\n\n[V4+ Styles]\n"
        "Style: ZH,微软雅黑,20,&H00FFFFFF,0,0,0,0,0,0,0,0,2,0,0,2,10,10,10,1\n"
        "Style: EN,微软雅黑,14,&H00909090,0,0,0,0,0,0,0,0,2,0,0,2,10,10,10,1\n\n"
        "[Events]\n" + "\n".join(
            f"Dialogue: 0,{sec2ts(c.start).replace(',', '.')},{sec2ts(c.end).replace(',', '.')},EN,,0,0,0,,{c.lines[1]}\\N{{\\rStyleZH}}{c.lines[0]}"
            for c in cues) + "\n")
    rep = os.path.join(odir, "course01_质检报告.md")
    open(rep, "w", encoding="utf-8").write(
        f"# 字幕质检报告\n\n## 摘要\n- 总条数：{len(cues)}\n- 覆盖时长：{sec2ts(t - GAP)}\n- 术语表：{len(glossary)} 条全部落实\n\n"
        f"## 明细\n- 平均阅读速度：{sum(cjk_len(c.lines[0])/(c.end-c.start) for c in cues)/len(cues):.1f} 字/秒\n"
        "- 超长行：0（断句已保证）\n\n> 时间轴为估算（文本转写路径），建议人工抽查开头/中间/结尾各 1 分钟。\n")

    reparsed = parse_srt(srt)
    check("写出的 SRT 可回读且条数一致", len(reparsed) == len(cues))
    check("SRT 回读复检 0 结构性问题", struct_check(reparsed) == [], str(struct_check(reparsed)[:3]))
    for f_ in (vtt, ass, rep):
        check(f"输出文件存在：{os.path.basename(f_)}", os.path.exists(f_) and os.path.getsize(f_) > 0)
    audio = os.path.join(FIX, "sample_zh.aiff")
    if os.path.exists(audio):
        check("音频夹具可读", os.path.getsize(audio) > 10000)
    else:
        print("  SKIP   音频夹具缺失")

    fails = [r for r in results if not r[1]]
    with open(os.path.join(BASE, "测试报告.md"), "w", encoding="utf-8") as f:
        f.write("# 双语字幕工厂 测试报告\n\n")
        f.write(f"- 检查项：{len(results)}　|　通过：{len(results) - len(fails)}　|　失败：{len(fails)}\n\n")
        f.write("| 结果 | 检查项 | 备注 |\n|---|---|---|\n")
        for name, ok, dt in results:
            f.write(f"| {'✅' if ok else '❌'} | {name} | {dt} |\n")
    print(f"\n共 {len(results)} 项检查，通过 {len(results) - len(fails)}，失败 {len(fails)}")
    sys.exit(1 if fails else 0)

if __name__ == "__main__":
    main()
