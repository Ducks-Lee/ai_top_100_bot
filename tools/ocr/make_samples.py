"""가짜 조합 장부·영수증 이미지와 정답 텍스트(truth.json)를 samples/에 만든다. 시험용이며 대회 데이터가 아니다."""
import json
import random
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

OUT = Path(__file__).parent / "samples"
OUT.mkdir(exist_ok=True)
FONT = "C:/Windows/Fonts/malgun.ttf"
random.seed(7)

LEDGER = [
    ["날짜", "조합원", "품목", "수량", "단위", "단가", "금액"],
    ["10-21", "김순자", "배추", "120", "kg", "1,350", "162,000"],
    ["10-21", "박영수", "고구마", "35", "박스", "18,500", "647,500"],
    ["10-22", "이말순", "대봉감", "48", "박스", "27,000", "1,296,000"],
    ["10-22", "최동호", "전어", "62", "kg", "9,800", "607,600"],
    ["10-23", "정갑례", "꽃게", "17", "kg", "32,400", "550,800"],
    ["10-23", "한기철", "새우젓", "9", "통", "45,000", "405,000"],
    ["10-24", "오복남", "무", "210", "kg", "780", "163,800"],
]
COLS = [30, 130, 250, 380, 460, 540, 680]


def ledger(path):
    font = ImageFont.truetype(FONT, 24)
    img = Image.new("RGB", (860, 60 + 46 * len(LEDGER)), "white")
    d = ImageDraw.Draw(img)
    d.text((30, 12), "교동마을 농산물 조합 입고 장부", font=font, fill="black")
    for r, row in enumerate(LEDGER):
        y = 60 + 46 * r
        d.line([(20, y - 4), (840, y - 4)], fill="gray")
        for x, cell in zip(COLS, row):
            d.text((x, y), cell, font=font, fill="black")
    img.save(path)
    lines = ["교동마을 농산물 조합 입고 장부"] + [" ".join(r) for r in LEDGER]
    return lines


RECEIPT = [
    "갯마을 수산물 조합",
    "위판 정산서",
    "일자 2026-10-24 06:40",
    "선주 문태식 (조합원번호 0417)",
    "삼치 12상자 x 54,000 = 648,000",
    "갈치 8상자 x 71,500 = 572,000",
    "오징어 25kg x 13,200 = 330,000",
    "합계 1,550,000",
    "수수료(4.5%) 69,750",
    "지급액 1,480,250",
]


def receipt(path):
    font = ImageFont.truetype(FONT, 22)
    img = Image.new("RGB", (560, 40 + 40 * len(RECEIPT)), (250, 248, 240))
    d = ImageDraw.Draw(img)
    for i, line in enumerate(RECEIPT):
        d.text((24, 20 + 40 * i), line, font=font, fill=(30, 30, 30))
    img.save(path)
    return RECEIPT


def degrade(src, path):
    img = Image.open(src).convert("RGB")
    img = img.rotate(3.5, expand=True, fillcolor="white", resample=Image.BICUBIC)
    img = img.resize((int(img.width * 0.8), int(img.height * 0.8)))
    img = img.filter(ImageFilter.GaussianBlur(1.0))
    px = img.load()
    for _ in range(img.width * img.height // 25):
        x, y = random.randrange(img.width), random.randrange(img.height)
        v = random.randint(90, 200)
        px[x, y] = (v, v, v)
    img.save(path, quality=70)


truth = {
    "ledger.png": ledger(OUT / "ledger.png"),
    "receipt.png": receipt(OUT / "receipt.png"),
}
degrade(OUT / "ledger.png", OUT / "ledger_noisy.jpg")
truth["ledger_noisy.jpg"] = truth["ledger.png"]
(OUT / "truth.json").write_text(json.dumps(truth, ensure_ascii=False, indent=1), encoding="utf-8")
print("ok")
