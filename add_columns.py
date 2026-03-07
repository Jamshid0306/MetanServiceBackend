import requests

url = "http://127.0.0.1:8000/products/create"

files = {
    "files": open("static/images/0d8333b29c574a898afc01cfb80ac95d.png", "rb")
}

data = {
    "name_uz": "Mahsulot UZ",
    "name_ru": "Продукт RU",
    "name_en": "Product EN",
    "description_uz": "Tavsif UZ",
    "description_ru": "Описание RU",
    "description_en": "Description EN",
    "characteristic_uz": "Xususiyatlar UZ",
    "characteristic_ru": "Характеристики RU",
    "characteristic_en": "Characteristics EN",
    "price": 100.5,
    "brand_name": "brand",
    "category_value": "torgovoe_oborudovanie"
}

response = requests.post(url, data=data, files=files)
print(response.status_code)
print(response.json())
