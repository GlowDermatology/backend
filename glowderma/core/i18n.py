"""The customer-audience message catalog (customer contract §5) and language resolution (§1.2).

``CATALOG`` holds every key of the contract with its Uzbek, Russian and English text; ``t(key, lang,
**params)`` renders one. The admin audience keeps its Uzbek strings (``core.api.exceptions.MESSAGES``); the
customer, public and POS views render through this module only. A package that needs a key that is not here
stops and reports it: the contract changes first (plan §0.1).
"""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation
from string import Formatter
from typing import Any, Final

LANGUAGES: Final[tuple[str, ...]] = ("uz", "ru", "en")
DEFAULT_LANGUAGE: Final = "uz"
NBSP: Final = " "
MINUS: Final = "−"

CATALOG: Final[dict[str, dict[str, str]]] = {
    "error.validation_error": {
        "uz": "Maydonlarni tekshiring.",
        "ru": "Проверьте поля.",
        "en": "Please check the fields.",
    },
    "error.bad_request": {
        "uz": "So‘rov noto‘g‘ri.",
        "ru": "Некорректный запрос.",
        "en": "Malformed request.",
    },
    "error.parse_error": {
        "uz": "So‘rovni o‘qib bo‘lmadi.",
        "ru": "Не удалось разобрать запрос.",
        "en": "The request could not be parsed.",
    },
    "error.not_authenticated": {
        "uz": "Avtorizatsiya talab qilinadi.",
        "ru": "Требуется авторизация.",
        "en": "Please sign in.",
    },
    "error.SESSION_EXPIRED": {
        "uz": "Sessiya tugadi. Qayta kiring.",
        "ru": "Сессия истекла. Войдите снова.",
        "en": "Your session has ended. Please sign in again.",
    },
    "error.SESSION_REPLACED": {
        "uz": "Hisobingizga boshqa qurilmadan kirildi.",
        "ru": "В ваш аккаунт вошли с другого устройства.",
        "en": "Your account was signed in on another device.",
    },
    "error.password_changed": {
        "uz": "Parolingiz o‘zgartirildi. Qayta kiring. Bu siz bo‘lmasangiz, sotuvchi bilan bog‘laning.",
        "ru": "Пароль изменён. Войдите снова. Если это были не вы, свяжитесь с продавцом.",
        "en": "Your password was changed. Please sign in again. If this wasn't you, contact the seller.",
    },
    "error.invalid_credentials": {
        "uz": "Telefon raqami yoki parol noto‘g‘ri.",
        "ru": "Неверный номер телефона или пароль.",
        "en": "Wrong phone number or password.",
    },
    "error.permission_denied": {
        "uz": "Ruxsat yo‘q.",
        "ru": "Доступ запрещён.",
        "en": "Access denied.",
    },
    "error.not_found": {
        "uz": "Topilmadi.",
        "ru": "Не найдено.",
        "en": "Not found.",
    },
    "error.method_not_allowed": {
        "uz": "Bu amal qo‘llab-quvvatlanmaydi.",
        "ru": "Действие не поддерживается.",
        "en": "This action is not supported.",
    },
    "error.not_acceptable": {
        "uz": "Javob formatini berib bo‘lmaydi.",
        "ru": "Невозможно вернуть ответ в этом формате.",
        "en": "The response format is not available.",
    },
    "error.unsupported_media_type": {
        "uz": "Ma’lumot formati noto‘g‘ri.",
        "ru": "Неверный формат данных.",
        "en": "Unsupported data format.",
    },
    "error.payload_too_large": {
        "uz": "Fayl juda katta.",
        "ru": "Файл слишком большой.",
        "en": "The file is too large.",
    },
    "error.device_required": {
        "uz": "Qurilma ma’lumotlari yuborilmadi. Ilovani yangilang.",
        "ru": "Не переданы данные устройства. Обновите приложение.",
        "en": "Device information is missing. Please update the app.",
    },
    "error.app_update_required": {
        "uz": "Ilovaning yangi versiyasini o‘rnating.",
        "ru": "Установите новую версию приложения.",
        "en": "Please install the latest version of the app.",
    },
    "error.idempotency_key_required": {
        "uz": "Idempotency-Key sarlavhasi kerak.",
        "ru": "Нужен заголовок Idempotency-Key.",
        "en": "The Idempotency-Key header is required.",
    },
    "error.idempotency_key_reused": {
        "uz": "Bu kalit boshqa so‘rov uchun ishlatilgan.",
        "ru": "Этот ключ уже использован для другого запроса.",
        "en": "This key was used for a different request.",
    },
    "error.request_in_progress": {
        "uz": "So‘rov bajarilmoqda. Birozdan so‘ng qayta urinib ko‘ring.",
        "ru": "Запрос выполняется. Повторите чуть позже.",
        "en": "The request is in progress. Please try again shortly.",
    },
    "error.throttled": {
        "uz": "Juda ko‘p urinish. {n} soniyadan so‘ng qayta urinib ko‘ring.",
        "ru": "Слишком много попыток. Повторите через {n} с.",
        "en": "Too many attempts. Try again in {n} s.",
    },
    "error.server_error": {
        "uz": "Kutilmagan xato.",
        "ru": "Непредвиденная ошибка.",
        "en": "Unexpected error.",
    },
    "error.provider_unavailable": {
        "uz": "Tashqi xizmat javob bermadi. Qayta urinib ko‘ring.",
        "ru": "Внешний сервис не ответил. Повторите попытку.",
        "en": "An external service did not respond. Please try again.",
    },
    "error.conflict": {
        "uz": "Amalni bajarib bo‘lmadi. Ma’lumotlarni yangilang.",
        "ru": "Не удалось выполнить действие. Обновите данные.",
        "en": "The action could not be completed. Please refresh.",
    },
    "error.otp_resend_too_soon": {
        "uz": "Yangi kodni {n} soniyadan so‘ng so‘rashingiz mumkin.",
        "ru": "Новый код можно запросить через {n} с.",
        "en": "You can request a new code in {n} s.",
    },
    "error.otp_daily_limit": {
        "uz": "Bugungi SMS kodlar limiti tugadi. Ertaga qayta urinib ko‘ring.",
        "ru": "Лимит SMS-кодов на сегодня исчерпан. Попробуйте завтра.",
        "en": "Today's SMS code limit is reached. Please try tomorrow.",
    },
    "error.otp_network_limit": {
        "uz": (
            "Bu tarmoqdan bugun juda ko‘p kod so‘raldi. Boshqa tarmoq "
            "(Wi-Fi yoki mobil internet) orqali urinib ko‘ring."
        ),
        "ru": (
            "С этой сети сегодня запрошено слишком много кодов. Попробуйте "
            "через другую сеть (Wi-Fi или мобильный интернет)."
        ),
        "en": (
            "Too many codes were requested from this network today. Please "
            "try over another network (Wi-Fi or mobile data)."
        ),
    },
    "error.otp_locked": {
        "uz": "Urinishlar soni tugadi. {m} daqiqadan so‘ng qayta urinib ko‘ring.",
        "ru": "Попытки закончились. Повторите через {m} мин.",
        "en": "No attempts left. Try again in {m} min.",
    },
    "error.otp_invalid": {
        "uz": "Kod noto‘g‘ri. Yana {n} marta urinishingiz mumkin.",
        "ru": "Неверный код. Осталось попыток: {n}.",
        "en": "Wrong code. {n} attempts left.",
    },
    "error.otp_expired": {
        "uz": "Kod muddati tugagan. Yangi kod oling.",
        "ru": "Срок действия кода истёк. Получите новый код.",
        "en": "The code has expired. Please request a new one.",
    },
    "error.sms_unavailable": {
        "uz": "SMS xizmati vaqtincha ishlamayapti. Birozdan so‘ng urinib ko‘ring.",
        "ru": "SMS-сервис временно недоступен. Попробуйте чуть позже.",
        "en": "The SMS service is temporarily unavailable. Please try again later.",
    },
    "error.verification_invalid": {
        "uz": "Tasdiqlash muddati tugagan. Kodni qaytadan oling.",
        "ru": "Подтверждение истекло. Получите код заново.",
        "en": "Verification has expired. Please request a new code.",
    },
    "error.phone_registered": {
        "uz": "Bu raqam ro‘yxatdan o‘tgan. Hisobingizga kiring.",
        "ru": "Этот номер уже зарегистрирован. Войдите в аккаунт.",
        "en": "This number is already registered. Please sign in.",
    },
    "error.phone_not_registered": {
        "uz": "Bu raqam bilan hisob topilmadi. Ro‘yxatdan o‘ting.",
        "ru": "Аккаунт с этим номером не найден. Зарегистрируйтесь.",
        "en": "No account uses this number. Please sign up.",
    },
    "error.terms_not_published": {
        "uz": "Ro‘yxatdan o‘tish vaqtincha mavjud emas. Keyinroq urinib ko‘ring.",
        "ru": "Регистрация временно недоступна. Попробуйте позже.",
        "en": "Sign-up is temporarily unavailable. Please try again later.",
    },
    "error.social_token_invalid": {
        "uz": "Kirish amalga oshmadi. Qayta urinib ko‘ring.",
        "ru": "Не удалось войти. Попробуйте ещё раз.",
        "en": "Sign-in failed. Please try again.",
    },
    "error.social_ticket_invalid": {
        "uz": "Ijtimoiy tarmoq orqali kirish muddati tugadi. Qaytadan boshlang.",
        "ru": "Время входа через соцсеть истекло. Начните заново.",
        "en": "Social sign-in has expired. Please start again.",
    },
    "error.social_provider_linked": {
        "uz": "Bu hisobga boshqa {provider} profili bog‘langan.",
        "ru": "К этому аккаунту уже привязан другой профиль {provider}.",
        "en": "Another {provider} profile is already linked to this account.",
    },
    "error.social_identity_linked": {
        "uz": "Bu {provider} profili boshqa hisobga bog‘langan.",
        "ru": "Этот профиль {provider} привязан к другому аккаунту.",
        "en": "This {provider} profile is linked to another account.",
    },
    "error.last_sign_in_method": {
        "uz": "Bu yagona kirish usuli. Avval parol o‘rnating.",
        "ru": "Это единственный способ входа. Сначала задайте пароль.",
        "en": "This is your only sign-in method. Please set a password first.",
    },
    "error.terms_update_required": {
        "uz": "Foydalanish shartlarining yangi tahririni qabul qiling.",
        "ru": "Примите новую редакцию условий использования.",
        "en": "Please accept the updated terms of use.",
    },
    "error.address_limit": {
        "uz": "Ko‘pi bilan {limit} ta manzil saqlash mumkin.",
        "ru": "Можно сохранить не более {limit} адресов.",
        "en": "You can save up to {limit} addresses.",
    },
    "error.product_unavailable": {
        "uz": "Mahsulot hozir sotuvda yo‘q.",
        "ru": "Товар сейчас недоступен.",
        "en": "This product is not available right now.",
    },
    "error.out_of_stock_one": {
        "uz": "Bu mahsulot qolmagan.",
        "ru": "Этот товар закончился.",
        "en": "This product is out of stock.",
    },
    "error.OUT_OF_STOCK": {
        "uz": "Ba’zi mahsulotlar yetarli emas.",
        "ru": "Некоторых товаров недостаточно.",
        "en": "Some products are not available in the requested quantity.",
    },
    "error.cart_full": {
        "uz": "Savatga ko‘pi bilan {limit} xil mahsulot qo‘shish mumkin.",
        "ru": "В корзину можно добавить не более {limit} разных товаров.",
        "en": "You can add up to {limit} different products to the cart.",
    },
    "error.cart_empty": {
        "uz": "Savat bo‘sh.",
        "ru": "Корзина пуста.",
        "en": "Your cart is empty.",
    },
    "error.delivery_unavailable": {
        "uz": "Bu hududga yetkazib berish mavjud emas.",
        "ru": "Доставка в этот регион недоступна.",
        "en": "Delivery to this area is not available.",
    },
    "error.delivery_not_offered": {
        "uz": "Yetkazib berish hozircha mavjud emas. Olib ketishni tanlang.",
        "ru": "Доставка пока недоступна. Выберите самовывоз.",
        "en": "Delivery is not available right now. Please choose pickup.",
    },
    "error.online_sales_unavailable": {
        "uz": "Onlayn buyurtma vaqtincha mavjud emas. Mahsulotlarni do‘konimizdan xarid qilishingiz mumkin.",
        "ru": "Онлайн-заказ временно недоступен. Товары можно купить в нашем магазине.",
        "en": "Online ordering is temporarily unavailable. You can buy the products in our store.",
    },
    "error.open_order_exists": {
        "uz": "Sizda to‘lanmagan buyurtma bor (№{order_id}). Avval uni to‘lang yoki bekor qiling.",
        "ru": "У вас есть неоплаченный заказ (№{order_id}). Оплатите или отмените его.",
        "en": "You have an unpaid order (№{order_id}). Please pay or cancel it first.",
    },
    "error.price_changed": {
        "uz": "Narxlar yangilandi. Buyurtmani qayta tekshiring.",
        "ru": "Цены обновились. Проверьте заказ ещё раз.",
        "en": "Prices have changed. Please review your order.",
    },
    "error.already_paid": {
        "uz": "Buyurtma allaqachon to‘langan.",
        "ru": "Заказ уже оплачен.",
        "en": "This order is already paid.",
    },
    "error.not_payable": {
        "uz": "Bu buyurtmani to‘lab bo‘lmaydi.",
        "ru": "Этот заказ нельзя оплатить.",
        "en": "This order cannot be paid.",
    },
    "error.order_expired": {
        "uz": "To‘lov muddati tugadi. Mahsulotlarni savatga qaytarib, qaytadan rasmiylashtiring.",
        "ru": "Время на оплату истекло. Верните товары в корзину и оформите заказ заново.",
        "en": "The payment time has expired. Put the products back in the cart and place the order again.",
    },
    "error.payment_in_progress": {
        "uz": "To‘lov {provider} ilovasida jarayonda. Uni yakunlang yoki biroz kuting.",
        "ru": "Оплата в {provider} уже идёт. Завершите её или подождите.",
        "en": "A {provider} payment is in progress. Please finish it or wait.",
    },
    "error.order_not_cancellable": {
        "uz": "Bu buyurtmani ilovadan bekor qilib bo‘lmaydi. Sotuvchi bilan bog‘laning.",
        "ru": "Этот заказ нельзя отменить в приложении. Свяжитесь с продавцом.",
        "en": "This order cannot be cancelled in the app. Please contact the seller.",
    },
    "error.not_cancellable": {
        "uz": "Buni bekor qilib bo‘lmaydi.",
        "ru": "Это нельзя отменить.",
        "en": "This cannot be cancelled.",
    },
    "error.invalid_transition": {
        "uz": "Bu amalni hozir bajarib bo‘lmaydi. Ma’lumotlarni yangilang.",
        "ru": "Это действие сейчас недоступно. Обновите данные.",
        "en": "This action is not available right now. Please refresh.",
    },
    "error.booking_limit": {
        "uz": "Sizda {limit} ta faol qabul bor. Yangisini olishdan oldin birini bekor qiling.",
        "ru": "У вас {limit} активных записи. Отмените одну, чтобы записаться снова.",
        "en": "You have {limit} active appointments. Cancel one to book another.",
    },
    "error.slot_taken": {
        "uz": "Bu vaqt band bo‘lib qoldi. Boshqa vaqtni tanlang.",
        "ru": "Это время уже занято. Выберите другое.",
        "en": "This time has just been taken. Please choose another.",
    },
    "error.appointment_not_cancellable": {
        "uz": "Bu qabulni bekor qilib bo‘lmaydi.",
        "ru": "Эту запись нельзя отменить.",
        "en": "This appointment cannot be cancelled.",
    },
    "error.qr_invalid": {
        "uz": "QR kod yaroqsiz yoki muddati o‘tgan. Mijozdan ilovada QR kodni yangilashni so‘rang.",
        "ru": "QR-код недействителен или устарел. Попросите клиента обновить его в приложении.",
        "en": "The QR code is invalid or expired. Ask the customer to refresh it in the app.",
    },
    "field.required": {
        "uz": "Majburiy maydon.",
        "ru": "Обязательное поле.",
        "en": "This field is required.",
    },
    "field.null": {
        "uz": "Majburiy maydon.",
        "ru": "Обязательное поле.",
        "en": "This field is required.",
    },
    "field.blank": {
        "uz": "Maydonni to‘ldiring.",
        "ru": "Заполните поле.",
        "en": "Please fill in this field.",
    },
    "field.invalid": {
        "uz": "Noto‘g‘ri qiymat.",
        "ru": "Неверное значение.",
        "en": "Invalid value.",
    },
    "field.invalid_choice": {
        "uz": "Ruxsat etilmagan qiymat.",
        "ru": "Недопустимое значение.",
        "en": "This value is not allowed.",
    },
    "field.min_length": {
        "uz": "Kamida {limit} ta belgi kiriting.",
        "ru": "Минимум {limit} символов.",
        "en": "At least {limit} characters.",
    },
    "field.max_length": {
        "uz": "{limit} belgidan oshmasin.",
        "ru": "Не более {limit} символов.",
        "en": "At most {limit} characters.",
    },
    "field.min_value": {
        "uz": "Qiymat {limit} dan kam bo‘lmasin.",
        "ru": "Не меньше {limit}.",
        "en": "At least {limit}.",
    },
    "field.max_value": {
        "uz": "Qiymat {limit} dan oshmasin.",
        "ru": "Не больше {limit}.",
        "en": "At most {limit}.",
    },
    "field.max_decimal_places": {
        "uz": "Ko‘pi bilan 2 ta kasr raqam.",
        "ru": "Не более 2 знаков после запятой.",
        "en": "At most 2 decimal places.",
    },
    "field.unique": {
        "uz": "Bu qiymat band.",
        "ru": "Это значение уже занято.",
        "en": "This value is already taken.",
    },
    "field.does_not_exist": {
        "uz": "Topilmadi.",
        "ru": "Не найдено.",
        "en": "Not found.",
    },
    "field.read_only": {
        "uz": "Bu maydonni o‘zgartirib bo‘lmaydi.",
        "ru": "Это поле нельзя изменить.",
        "en": "This field cannot be changed.",
    },
    "field.file_too_large": {
        "uz": "Rasm 5 MB dan oshmasin.",
        "ru": "Изображение не больше 5 МБ.",
        "en": "The image must be at most 5 MB.",
    },
    "field.unsupported_image": {
        "uz": "Faqat JPEG, PNG yoki WebP rasm.",
        "ru": "Только изображения JPEG, PNG или WebP.",
        "en": "JPEG, PNG or WebP images only.",
    },
    "field.max_items": {
        "uz": "Ko‘pi bilan {limit} ta.",
        "ru": "Не более {limit}.",
        "en": "At most {limit}.",
    },
    "field.mismatch": {
        "uz": "Ro‘yxat joriy elementlarga mos emas.",
        "ru": "Список не совпадает с текущими элементами.",
        "en": "The list does not match the current items.",
    },
    "field.phone_invalid": {
        "uz": "Telefon raqamini to‘g‘ri kiriting.",
        "ru": "Введите корректный номер телефона.",
        "en": "Please enter a valid phone number.",
    },
    "field.otp_format": {
        "uz": "6 xonali kodni kiriting.",
        "ru": "Введите 6-значный код.",
        "en": "Enter the 6-digit code.",
    },
    "field.otp_invalid": {
        "uz": "Kod noto‘g‘ri. Yana {n} marta urinishingiz mumkin.",
        "ru": "Неверный код. Осталось попыток: {n}.",
        "en": "Wrong code. {n} attempts left.",
    },
    "field.social_ticket_required": {
        "uz": "Ijtimoiy tarmoq orqali kirishni qaytadan boshlang.",
        "ru": "Начните вход через соцсеть заново.",
        "en": "Please start social sign-in again.",
    },
    "field.name_min": {
        "uz": "Ismingizni kiriting (kamida 2 belgi).",
        "ru": "Введите имя (минимум 2 символа).",
        "en": "Enter your name (at least 2 characters).",
    },
    "field.password_too_short": {
        "uz": "Parol kamida {limit} ta belgidan iborat bo‘lsin.",
        "ru": "Пароль должен быть не короче {limit} символов.",
        "en": "The password must be at least {limit} characters.",
    },
    "field.password_too_common": {
        "uz": "Bu parol juda oddiy.",
        "ru": "Этот пароль слишком простой.",
        "en": "This password is too common.",
    },
    "field.password_entirely_numeric": {
        "uz": "Parol faqat raqamlardan iborat bo‘lmasin.",
        "ru": "Пароль не должен состоять только из цифр.",
        "en": "The password cannot be only digits.",
    },
    "field.password_too_similar": {
        "uz": "Parol telefon raqamingizga juda o‘xshash.",
        "ru": "Пароль слишком похож на номер телефона.",
        "en": "The password is too similar to your phone number.",
    },
    "field.password_unchanged": {
        "uz": "Yangi parol eskisidan farq qilsin.",
        "ru": "Новый пароль должен отличаться от старого.",
        "en": "The new password must differ from the old one.",
    },
    "field.password_incorrect": {
        "uz": "Parol noto‘g‘ri.",
        "ru": "Неверный пароль.",
        "en": "Wrong password.",
    },
    "field.terms_outdated": {
        "uz": "Foydalanish shartlarining yangi tahririni qabul qiling.",
        "ru": "Примите новую редакцию условий использования.",
        "en": "Please accept the updated terms of use.",
    },
    "field.push_token_missing": {
        "uz": "Bildirishnomalarni yoqish uchun ilovaga ruxsat bering.",
        "ru": "Разрешите приложению уведомления, чтобы их включить.",
        "en": "Allow notifications for the app to turn them on.",
    },
    "field.address_text_min": {
        "uz": "Manzilni to‘liq kiriting.",
        "ru": "Введите полный адрес.",
        "en": "Please enter the full address.",
    },
    "field.latlng_pair": {
        "uz": "Joylashuvni qayta tanlang.",
        "ru": "Выберите местоположение заново.",
        "en": "Please choose the location again.",
    },
    "field.address_required": {
        "uz": "Yetkazib berish manzilini tanlang.",
        "ru": "Выберите адрес доставки.",
        "en": "Choose a delivery address.",
    },
    "field.address_not_found": {
        "uz": "Manzil topilmadi.",
        "ru": "Адрес не найден.",
        "en": "Address not found.",
    },
    "field.address_incomplete": {
        "uz": "Manzilda viloyat va tumanni tanlang.",
        "ru": "Укажите в адресе область и район.",
        "en": "Choose the region and district in the address.",
    },
    "field.cashback_insufficient": {
        "uz": "Cashback balansi yetarli emas (mavjud: {available} so‘m).",
        "ru": "Недостаточно кешбэка (доступно: {available} сум).",
        "en": "Not enough cashback (available: {available} so'm).",
    },
    "field.cashback_max": {
        "uz": "Ko‘pi bilan {max} so‘m cashback ishlatish mumkin.",
        "ru": "Можно использовать не более {max} сум кешбэка.",
        "en": "You can use at most {max} so'm of cashback.",
    },
    "field.specialist_not_found": {
        "uz": "Mutaxassis topilmadi.",
        "ru": "Специалист не найден.",
        "en": "Specialist not found.",
    },
    "field.service_not_offered": {
        "uz": "Mutaxassis bu xizmatni ko‘rsatmaydi.",
        "ru": "Специалист не оказывает эту услугу.",
        "en": "The specialist does not offer this service.",
    },
    "field.booking_too_soon": {
        "uz": "Kamida {n} daqiqa oldin yoziling.",
        "ru": "Записывайтесь не позднее чем за {n} мин.",
        "en": "Book at least {n} minutes in advance.",
    },
    "field.booking_horizon": {
        "uz": "Faqat {n} kun ichidagi sanani tanlash mumkin.",
        "ru": "Можно выбрать дату не далее {n} дней.",
        "en": "You can book up to {n} days ahead.",
    },
    "field.booking_grid": {
        "uz": "Vaqt {n} daqiqalik qadamda bo‘lsin.",
        "ru": "Время должно быть кратно {n} минутам.",
        "en": "The time must be on a {n}-minute step.",
    },
    "field.outside_working_hours": {
        "uz": "Mutaxassis bu vaqtda ishlamaydi.",
        "ru": "Специалист в это время не работает.",
        "en": "The specialist does not work at this time.",
    },
    "field.slot_unavailable": {
        "uz": "Bu vaqt band.",
        "ru": "Это время занято.",
        "en": "This time is taken.",
    },
    "field.own_overlap": {
        "uz": "Sizda bu vaqtda boshqa qabul bor.",
        "ru": "У вас уже есть запись на это время.",
        "en": "You already have an appointment at this time.",
    },
    "label.order_status.awaiting_payment": {
        "uz": "To‘lov kutilmoqda",
        "ru": "Ожидает оплаты",
        "en": "Awaiting payment",
    },
    "label.order_status.processing": {
        "uz": "Tayyorlanmoqda",
        "ru": "Готовится",
        "en": "Being prepared",
    },
    "label.order_status.handed_to_courier": {
        "uz": "Kuryerga topshirildi",
        "ru": "Передан курьеру",
        "en": "With the courier",
    },
    "label.order_status.delivered": {
        "uz": "Yetkazildi",
        "ru": "Доставлен",
        "en": "Delivered",
    },
    "label.order_status_pickup.delivered": {
        "uz": "Olib ketildi",
        "ru": "Получен",
        "en": "Picked up",
    },
    "label.order_status.cancelled": {
        "uz": "Bekor qilindi",
        "ru": "Отменён",
        "en": "Cancelled",
    },
    "label.payment_status.pending": {
        "uz": "To‘lov kutilmoqda",
        "ru": "Ожидает оплаты",
        "en": "Awaiting payment",
    },
    "label.payment_status.paid": {
        "uz": "To‘langan",
        "ru": "Оплачен",
        "en": "Paid",
    },
    "label.payment_status.failed": {
        "uz": "To‘lov o‘tmadi",
        "ru": "Оплата не прошла",
        "en": "Payment failed",
    },
    "label.payment_status.refunded": {
        "uz": "Qaytarilgan",
        "ru": "Возвращён",
        "en": "Refunded",
    },
    "label.payment.in_progress": {
        "uz": "To‘lov tekshirilmoqda",
        "ru": "Оплата проверяется",
        "en": "Payment is being confirmed",
    },
    "label.fulfilment.delivery": {
        "uz": "Yetkazib berish",
        "ru": "Доставка",
        "en": "Delivery",
    },
    "label.fulfilment.pickup": {
        "uz": "Olib ketish",
        "ru": "Самовывоз",
        "en": "Pickup",
    },
    "label.cancellation.reservation_expired": {
        "uz": "To‘lov muddati tugadi",
        "ru": "Истекло время оплаты",
        "en": "Payment time expired",
    },
    "label.cancellation.admin": {
        "uz": "Do‘kon bekor qildi",
        "ru": "Отменён магазином",
        "en": "Cancelled by the shop",
    },
    "label.cancellation.customer_request": {
        "uz": "Siz bekor qildingiz",
        "ru": "Отменён вами",
        "en": "Cancelled by you",
    },
    "label.refund.in_progress": {
        "uz": "To‘lov qaytarilmoqda",
        "ru": "Возврат в процессе",
        "en": "Refund in progress",
    },
    "label.refund.completed": {
        "uz": "To‘lov qaytarildi",
        "ru": "Деньги возвращены",
        "en": "Refunded",
    },
    "label.timeline.order_created": {
        "uz": "Buyurtma berildi",
        "ru": "Заказ оформлен",
        "en": "Order placed",
    },
    "label.timeline.sub_items_total": {
        "uz": "{item_count} ta mahsulot · {total} so‘m",
        "ru": "{item_count} товар(ов) · {total} сум",
        "en": "{item_count} item(s) · {total} so'm",
    },
    "label.timeline.payment_confirmed": {
        "uz": "To‘lov qabul qilindi",
        "ru": "Оплата получена",
        "en": "Payment received",
    },
    "label.timeline.sub_paid_via": {
        "uz": "{provider} orqali",
        "ru": "через {provider}",
        "en": "via {provider}",
    },
    "label.timeline.sub_cashback": {
        "uz": "cashback +{amount} so‘m",
        "ru": "кешбэк +{amount} сум",
        "en": "cashback +{amount} so'm",
    },
    "label.timeline.payment_failed": {
        "uz": "To‘lov amalga oshmadi",
        "ru": "Оплата не прошла",
        "en": "Payment failed",
    },
    "label.timeline.sub_payment_failed_retry": {
        "uz": "{provider} · qayta urinib ko‘ring",
        "ru": "{provider} · попробуйте ещё раз",
        "en": "{provider} · please try again",
    },
    "label.timeline.sub_provider": {
        "uz": "{provider}",
        "ru": "{provider}",
        "en": "{provider}",
    },
    "label.timeline.shipment_created": {
        "uz": "Yetkazib berishga topshirildi",
        "ru": "Передан в доставку",
        "en": "Handed over for delivery",
    },
    "label.timeline.sub_tracking": {
        "uz": "BTS kuzatuv raqami: {tracking_number}",
        "ru": "Трек-номер BTS: {tracking_number}",
        "en": "BTS tracking number: {tracking_number}",
    },
    "label.timeline.handed_to_courier": {
        "uz": "Kuryerga topshirildi",
        "ru": "Передан курьеру",
        "en": "With the courier",
    },
    "label.timeline.delivered": {
        "uz": "Yetkazildi",
        "ru": "Доставлен",
        "en": "Delivered",
    },
    "label.timeline.picked_up": {
        "uz": "Olib ketildi",
        "ru": "Получен",
        "en": "Picked up",
    },
    "label.timeline.sub_bts": {
        "uz": "BTS",
        "ru": "BTS",
        "en": "BTS",
    },
    "label.timeline.cancelled": {
        "uz": "Buyurtma bekor qilindi",
        "ru": "Заказ отменён",
        "en": "Order cancelled",
    },
    "label.timeline.sub_cancelled_admin": {
        "uz": "Do‘kon tomonidan bekor qilindi",
        "ru": "Отменён магазином",
        "en": "Cancelled by the shop",
    },
    "label.timeline.sub_cancelled_customer": {
        "uz": "Siz bekor qildingiz",
        "ru": "Вы отменили заказ",
        "en": "You cancelled the order",
    },
    "label.timeline.sub_refund_follows": {
        "uz": "to‘lov qaytariladi",
        "ru": "деньги будут возвращены",
        "en": "the payment will be refunded",
    },
    "label.timeline.sub_expired": {
        "uz": "To‘lov muddati tugadi",
        "ru": "Истекло время оплаты",
        "en": "The payment time expired",
    },
    "label.timeline.refund_in_progress": {
        "uz": "To‘lov qaytarilmoqda",
        "ru": "Возврат в процессе",
        "en": "Refund in progress",
    },
    "label.timeline.sub_amount": {
        "uz": "{amount} so‘m",
        "ru": "{amount} сум",
        "en": "{amount} so'm",
    },
    "label.timeline.refund_succeeded": {
        "uz": "To‘lov qaytarildi",
        "ru": "Деньги возвращены",
        "en": "Payment refunded",
    },
    "label.timeline.sub_amount_provider": {
        "uz": "{amount} so‘m · {provider}",
        "ru": "{amount} сум · {provider}",
        "en": "{amount} so'm · {provider}",
    },
    "label.progress.placed": {
        "uz": "Buyurtma berildi",
        "ru": "Оформлен",
        "en": "Placed",
    },
    "label.progress.paid": {
        "uz": "To‘lov qabul qilindi",
        "ru": "Оплачен",
        "en": "Paid",
    },
    "label.progress.with_courier": {
        "uz": "Kuryerda",
        "ru": "У курьера",
        "en": "With courier",
    },
    "label.progress.delivered": {
        "uz": "Yetkazildi",
        "ru": "Доставлен",
        "en": "Delivered",
    },
    "label.progress.picked_up": {
        "uz": "Olib ketildi",
        "ru": "Получен",
        "en": "Picked up",
    },
    "label.progress.cancelled": {
        "uz": "Bekor qilindi",
        "ru": "Отменён",
        "en": "Cancelled",
    },
    "label.ledger.credit": {
        "uz": "Cashback hisoblandi",
        "ru": "Начислен кешбэк",
        "en": "Cashback earned",
    },
    "label.ledger.debit": {
        "uz": "Cashback ishlatildi",
        "ru": "Списан кешбэк",
        "en": "Cashback used",
    },
    "label.ledger.reversal_back": {
        "uz": "Cashback qaytarildi",
        "ru": "Кешбэк возвращён",
        "en": "Cashback returned",
    },
    "label.ledger.reversal_taken": {
        "uz": "Cashback bekor qilindi",
        "ru": "Кешбэк отменён",
        "en": "Cashback reversed",
    },
    "label.ledger.adjust_add": {
        "uz": "Bonus qo‘shildi",
        "ru": "Начислен бонус",
        "en": "Bonus added",
    },
    "label.ledger.adjust_subtract": {
        "uz": "Balans tuzatildi",
        "ru": "Баланс скорректирован",
        "en": "Balance corrected",
    },
    "label.ledger.sub_order_percent": {
        "uz": "Buyurtma №{order_id} · {percent}%",
        "ru": "Заказ №{order_id} · {percent}%",
        "en": "Order №{order_id} · {percent}%",
    },
    "label.ledger.sub_store_percent": {
        "uz": "Do‘kondagi xarid · {percent}%",
        "ru": "Покупка в магазине · {percent}%",
        "en": "In-store purchase · {percent}%",
    },
    "label.ledger.sub_order": {
        "uz": "Buyurtma №{order_id}",
        "ru": "Заказ №{order_id}",
        "en": "Order №{order_id}",
    },
    "label.ledger.sub_order_cancelled": {
        "uz": "Buyurtma №{order_id} bekor qilindi",
        "ru": "Заказ №{order_id} отменён",
        "en": "Order №{order_id} cancelled",
    },
    "label.ledger.sub_store_returned": {
        "uz": "Do‘kondagi xarid qaytarildi",
        "ru": "Покупка в магазине возвращена",
        "en": "In-store purchase returned",
    },
    "label.ledger.sub_previous": {
        "uz": "Avvalgi hisob",
        "ru": "Прежний аккаунт",
        "en": "Previous account",
    },
    "label.cashback.promo_rule": {
        "uz": "Aksiyadagi mahsulotlarga cashback berilmaydi.",
        "ru": "На акционные товары кешбэк не начисляется.",
        "en": "Promotional products do not earn cashback.",
    },
    "label.appointment_status.requested": {
        "uz": "Tasdiqlanishi kutilmoqda",
        "ru": "Ожидает подтверждения",
        "en": "Awaiting confirmation",
    },
    "label.appointment_status.confirmed": {
        "uz": "Tasdiqlandi",
        "ru": "Подтверждена",
        "en": "Confirmed",
    },
    "label.appointment_status.rejected": {
        "uz": "Rad etildi",
        "ru": "Отклонена",
        "en": "Declined",
    },
    "label.appointment_status.attended": {
        "uz": "Qabul bo‘lib o‘tdi",
        "ru": "Состоялась",
        "en": "Completed",
    },
    "label.appointment_status.cancelled": {
        "uz": "Bekor qilindi",
        "ru": "Отменена",
        "en": "Cancelled",
    },
    "label.role.dermatologist": {
        "uz": "Dermatolog",
        "ru": "Дерматолог",
        "en": "Dermatologist",
    },
    "label.role.cosmetologist": {
        "uz": "Kosmetolog",
        "ru": "Косметолог",
        "en": "Cosmetologist",
    },
    "push.account_security.social_linked.title": {
        "uz": "Hisobingizga yangi kirish usuli qo‘shildi",
        "ru": "Добавлен новый способ входа",
        "en": "A new sign-in method was added",
    },
    "push.account_security.social_linked.body": {
        "uz": "Hisobingizga {provider} profili bog‘landi. Agar bu siz bo‘lmasangiz, parolni tiklang.",
        "ru": "К аккаунту привязан профиль {provider}. Если это были не вы, восстановите пароль.",
        "en": "A {provider} profile was linked to your account. If this wasn't you, reset your password.",
    },
    "push.account_security.social_unlinked.title": {
        "uz": "Kirish usuli o‘chirildi",
        "ru": "Способ входа удалён",
        "en": "A sign-in method was removed",
    },
    "push.account_security.social_unlinked.body": {
        "uz": "{provider} profili hisobingizdan uzildi.",
        "ru": "Профиль {provider} отвязан от аккаунта.",
        "en": "The {provider} profile was unlinked from your account.",
    },
    "push.account_security.password_reset.title": {
        "uz": "Parol o‘zgartirildi",
        "ru": "Пароль изменён",
        "en": "Password changed",
    },
    "push.account_security.password_reset.body": {
        "uz": "Hisobingiz paroli yangilandi. Agar bu siz bo‘lmasangiz, sotuvchi bilan bog‘laning.",
        "ru": "Пароль аккаунта обновлён. Если это были не вы, свяжитесь с продавцом.",
        "en": "Your account password was changed. If this wasn't you, contact the seller.",
    },
    "push.account_security.password_changed.title": {
        "uz": "Parol o‘rnatildi",
        "ru": "Пароль задан",
        "en": "Password set",
    },
    "push.account_security.password_changed.body": {
        "uz": "Hisobingiz uchun yangi parol o‘rnatildi. Agar bu siz bo‘lmasangiz, sotuvchi bilan bog‘laning.",
        "ru": "Для аккаунта задан новый пароль. Если это были не вы, свяжитесь с продавцом.",
        "en": "A new password was set for your account. If this wasn't you, contact the seller.",
    },
    "sms.otp": {
        "uz": "Glow Derma: tasdiqlash kodingiz {code}. Kodni hech kimga bermang.",
        "ru": "Glow Derma: ваш код подтверждения {code}. Никому его не сообщайте.",
        "en": "Glow Derma: your verification code is {code}. Do not share it with anyone.",
    },
}


def placeholders(template: str) -> set[str]:
    """The ``{name}`` placeholders of a template."""
    return {name for _literal, name, _spec, _conv in Formatter().parse(template) if name}


def normalize_language(value: Any) -> str:
    """``value`` when it is a supported language, else ``uz``."""
    return value if isinstance(value, str) and value in LANGUAGES else DEFAULT_LANGUAGE


def t(key: str, lang: str | None = None, **params: Any) -> str:
    """Render ``key`` in ``lang`` (unsupported or ``None`` → ``uz``) with ``params``.

    An unknown key or a missing placeholder raises ``KeyError``: both are programming errors that must show up
    where the message is raised, never as a half-rendered text on a phone.
    """
    texts = CATALOG[key]
    template = texts[normalize_language(lang)]
    names = placeholders(template)
    missing = names - set(params)
    if missing:
        raise KeyError(f"{key} needs {sorted(missing)}")
    return template.format(**{name: params[name] for name in names})


_LANGUAGE_RANGE = re.compile(r"^\s*([A-Za-z]{1,8})(?:-[A-Za-z0-9]{1,8})*\s*$")


def _parse_accept_language(header: str) -> list[str]:
    """Supported primary tags of an ``Accept-Language`` header in q-order (stable for equal weights)."""
    ranked: list[tuple[float, int, str]] = []
    for index, part in enumerate(header.split(",")):
        tag, _sep, params = part.partition(";")
        match = _LANGUAGE_RANGE.match(tag)
        if match is None:
            continue
        weight = 1.0
        for param in params.split(";"):
            name, _eq, value = param.strip().partition("=")
            if name.lower() == "q":
                try:
                    weight = float(value)
                except ValueError:
                    weight = 0.0
        language = match.group(1).lower()
        if weight > 0 and language in LANGUAGES:
            ranked.append((-weight, index, language))
    return [language for _weight, _index, language in sorted(ranked)]


def resolve_language(request: Any, customer: Any = None) -> str:
    """The response language (customer contract §1.2).

    The first supported tag of ``Accept-Language`` in q-order (region subtags ignored: ``ru-RU`` → ``ru``);
    absent or unsupported → ``customer.language`` when a customer is given, else ``uz``. It never reads
    ``request.user`` (on a DRF request that would run authentication again); callers pass the customer.
    """
    meta = getattr(request, "META", None)
    header = meta.get("HTTP_ACCEPT_LANGUAGE", "") if isinstance(meta, dict) else ""
    ranked = _parse_accept_language(str(header)) if header else []
    if ranked:
        return ranked[0]
    if customer is not None:
        return normalize_language(getattr(customer, "language", None))
    return DEFAULT_LANGUAGE


def format_number(value: int, lang: str | None = None) -> str:
    """Thousands-grouped integer: NBSP and U+2212 minus for uz/ru (the admin ``N()`` rule), comma for en."""
    grouped = f"{abs(int(value)):,}"
    if normalize_language(lang) == "en":
        return ("-" if value < 0 else "") + grouped
    return (MINUS if value < 0 else "") + grouped.replace(",", NBSP)


def format_percent(value: Any, lang: str | None = None) -> str:
    """A percent without trailing zeros: ``"1.50"`` → ``1,5`` (uz/ru, the admin ``PCT()`` rule) / ``1.5``
    (en).
    """
    try:
        number = Decimal(str(value))
    except InvalidOperation as exc:
        raise ValueError(f"Not a percent value: {value!r}") from exc
    text = format(number.normalize(), "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    if text in ("", "-0"):
        text = "0"
    return text if normalize_language(lang) == "en" else text.replace(".", ",")
