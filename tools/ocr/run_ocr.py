"""PaddleOCR(한국어)로 이미지를 읽는다.

사용: .venv-ocr/Scripts/python tools/ocr/run_ocr.py 이미지 [이미지 ...]   → 이미지별 .txt(줄 단위 결과) 저장
      PowerShell에서는 먼저 $env:PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK="True" (모델 출처 확인 생략)
      .venv-ocr는 Python 3.13 + paddlepaddle 3.3.1(CPU) + paddleocr 3.7.0. 봇(.venv, EC2)에는 넣지 않는다.
      --eval 을 붙이면 같은 폴더의 truth.json과 비교해 정확도를 출력한다.
"""
import difflib
import json
import math
import re
import statistics
import sys
import time
from collections import Counter
from pathlib import Path

t0 = time.time()
from paddleocr import PaddleOCR

ocr = PaddleOCR(
    lang="korean",
    use_doc_orientation_classify=False,
    use_doc_unwarping=False,
    use_textline_orientation=False,
    enable_mkldnn=False,  # paddle 3.3 CPU oneDNN 오류 회피
)
print(f"init {time.time() - t0:.1f}s")

args = [a for a in sys.argv[1:] if a != "--eval"]
evaluate = "--eval" in sys.argv


def skew(polys):
    """글자 상자 윗변·아랫변 기울기의 중앙값(라디안). 사진이 기울어도 같은 행을 묶기 위해 쓴다."""
    angles = []
    for p in polys:
        for (x0, y0), (x1, y1) in ((p[0], p[1]), (p[3], p[2])):
            if x1 - x0 > 20:  # 짧은 상자는 기울기가 부정확하다
                angles.append(math.atan2(y1 - y0, x1 - x0))
    return statistics.median(angles) if angles else 0.0


def read(path):
    t = time.time()
    res = ocr.predict(str(path))[0]
    texts, polys = res["rec_texts"], [p.tolist() for p in res["rec_polys"]]
    if not texts:
        return [], time.time() - t
    a = skew(polys)
    cos, sin = math.cos(a), math.sin(a)
    items = []
    for text, p in zip(texts, polys):
        cx = sum(x for x, _ in p) / 4
        cy = sum(y for _, y in p) / 4
        h = (math.dist(p[0], p[3]) + math.dist(p[1], p[2])) / 2
        # 기울기만큼 되돌린 좌표: 같은 행이면 v가 거의 같다
        items.append((cy * cos - cx * sin, cx * cos + cy * sin, h, text))
    # 위에서 아래, 같은 줄은 왼쪽부터. 행 평균 높이와 글자 높이 절반 넘게 차이 나면 새 줄
    tol = statistics.median(h for _, _, h, _ in items) / 2
    lines, cur = [], []
    for v, u, _, text in sorted(items):
        if cur and abs(v - statistics.mean(c[0] for c in cur)) > tol:
            lines.append(" ".join(t for _, _, t in sorted(cur, key=lambda c: c[1])))
            cur = []
        cur.append((v, u, text))
    if cur:
        lines.append(" ".join(t for _, _, t in sorted(cur, key=lambda c: c[1])))
    return lines, time.time() - t


truth = {}
if evaluate:
    truth = json.loads((Path(args[0]).parent / "truth.json").read_text(encoding="utf-8"))

for a in args:
    p = Path(a)
    lines, sec = read(p)
    p.with_suffix(".txt").write_text("\n".join(lines), encoding="utf-8")
    print(f"\n== {p.name}  {sec:.1f}s")
    for l in lines:
        print("  ", l)
    if p.name in truth:
        gt = "\n".join(truth[p.name])
        got = "\n".join(lines)
        char = difflib.SequenceMatcher(None, gt.replace(" ", ""), got.replace(" ", "")).ratio()
        # 같은 값이 여러 번 나와도 한 번씩 차감해 누락을 센다
        nums_gt = re.findall(r"[\d][\d,.:\-]*", gt)
        missing = list((Counter(nums_gt) - Counter(re.findall(r"[\d][\d,.:\-]*", got))).elements())
        words_gt = re.findall(r"[가-힣]+", gt)
        wrong_words = list((Counter(words_gt) - Counter(re.findall(r"[가-힣]+", got))).elements())
        print(f"   글자 일치율 {char:.1%}")
        print(f"   숫자 {len(nums_gt) - len(missing)}/{len(nums_gt)} 정확, 틀림/누락: {missing}")
        print(f"   한글 단어 {len(words_gt) - len(wrong_words)}/{len(words_gt)} 정확, 틀림/누락: {wrong_words}")
