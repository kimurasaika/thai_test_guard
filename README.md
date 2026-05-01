# 🛡️🍜 Allergy Alert — Thai Food Allergy Detector

เว็บถ่ายรูปเมนูอาหาร → ใช้ **Typhoon OCR** อ่านชื่อเมนู → ตรวจวัตถุดิบ → แจ้งเตือนถ้ามีสารก่อภูมิแพ้

## โครงสร้าง

```
allergy-app/
├── backend/main.py          FastAPI + Typhoon OCR/LLM
├── frontend/                HTML/CSS/JS
├── data/
│   ├── allergy.json         รายการสารก่อภูมิแพ้
│   └── recipe.json          ฐานข้อมูลเมนูอาหาร
├── requirements.txt
└── .env.example
```

## ติดตั้ง

```bash
cd allergy-app
python -m venv .venv
source .venv/bin/activate     # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env
# แก้ .env ใส่ TYPHOON_API_KEY ที่ได้จาก https://opentyphoon.ai
```

## รัน

```bash
cd backend
python main.py
# เปิด http://localhost:8000
```

## Flow

1. ผู้ใช้ติ๊กสารก่อภูมิแพ้ที่ตัวเองแพ้ (เก็บใน localStorage)
2. ถ่ายรูป/อัปโหลดเมนู หรือพิมพ์ชื่อเมนูเอง
3. Backend ส่งรูปให้ Typhoon OCR → ได้ข้อความชื่อเมนู
4. Match กับ `data/foods.json` → ถ้าเจอใช้ข้อมูลจาก DB
5. ถ้าไม่เจอ ส่งให้ Typhoon LLM ช่วยบอกวัตถุดิบ + สารก่อภูมิแพ้
6. เปรียบเทียบกับสารก่อภูมิแพ้ที่ผู้ใช้ติ๊กไว้ → แจ้งเตือนสีแดงถ้ามี

## สารก่อภูมิแพ้ที่รองรับ

ถั่วลิสง, ถั่วเปลือกแข็ง, กุ้ง/ปู/หอย, ปลา/น้ำปลา, ไข่, นม, แป้งสาลี/กลูเตน, ถั่วเหลือง/ซีอิ๊ว, งา, MSG, กะทิ, พริก/เผ็ด

## ขยายฐานข้อมูล

**เพิ่มเมนูใหม่** ใน `data/recipe.json` ที่ array `dishes`:

```json
{
  "name_th": "ชื่อไทย",
  "name_en": "English",
  "aliases": ["ชื่ออื่นๆ"],
  "ingredients": ["วัตถุดิบ1", "วัตถุดิบ2"],
  "allergens": ["peanut", "egg"]
}
```

**เพิ่มสารก่อภูมิแพ้ใหม่** ใน `data/allergy.json` ที่ object `allergens`:

```json
"key_name": {
  "th": "ชื่อไทย",
  "en": "English Name",
  "icon": "🥜",
  "keywords": ["คำที่เกี่ยวข้อง"]
}
```

ค่า `allergens` ของแต่ละเมนูใน `recipe.json` ต้องอ้างถึง key ที่มีใน `allergy.json` เท่านั้น
