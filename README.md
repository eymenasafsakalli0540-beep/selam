# Selam

Telefon numarası istemeyen sohbet uygulaması. E-postana gelen 6 haneli kodla giriş yaparsın, bir kez kullanıcı adı seçersin, arkadaşlarını kullanıcı adıyla bulursun.

## Çalıştırma (Windows)

1. Zip'i **Tümünü ayıkla** ile aç. İçinden çıkan klasörde `server.py` dosyasını görmelisin (iç içe klasör varsa içindekine gir).
2. `Selam-Baslat.bat` dosyasına çift tıkla. İlk seferde gerekli paketleri kurar ve sunucuyu başlatır.
3. Tarayıcıda http://localhost:3000 adresini aç.

Elle çalıştırmak istersen: `python -m pip install -r requirements.txt` ve `python server.py`.

Açılışta pencere sana ne olduğunu söyler: hangi `.env` dosyasının okunduğunu, e-postanın ayarlı olup olmadığını.

## Kodun e-postaya gelmesi

`.env` ayarlanmadıysa kod maile **gelmez**, `Selam-Baslat.bat` penceresine yazılır.

1. `.env.example` dosyasını kopyala ve adını `.env` yap. **`server.py` ile aynı klasörde olmalı**, adında `.txt` olmamalı.
2. Not Defteri ile aç, Gmail adresini ve uygulama şifreni yaz (Google hesabı > 2 adımlı doğrulama > https://myaccount.google.com/apppasswords).
3. `Mail-Test.bat` ile kendi adresine test maili gönder. Olmazsa pencere nedenini Türkçe yazar.
4. `Selam-Baslat.bat` ile uygulamayı yeniden başlat.

`.env` dosyasında şifren var: kimseyle paylaşma, zip'leyip gönderme.

## Özellikler

- E-posta kodu ile giriş, oturum açık kalır (çıkış yapana kadar tekrar kod istemez)
- Arama: kişileri kullanıcı adının herhangi bir yerinden bul, eski mesajları ara (ş=s, ı=i gibi Türkçe karakter farkı yok sayılır), bulunan mesaja git
- 🔗 Davet linki: arkadaşın linke tıklayıp giriş yapınca seninle sohbet açılır
- 📞 Sesli arama (telefon görüşmesi gibi): çalma sesi, kabul/reddet, sessize alma, süre; cevapsız/reddedilen aramalar sohbette görünür. Aramak için önce o kişiyle mesajlaşmış olmak gerekir. Mikrofon için **https** adres (ya da localhost) şarttır, bu yüzden aynı Wi-Fi'de `http://192.168...` adresinde arama çalışmaz, internet sürümünde ve Cloudflare tünelinde çalışır
- Anlık mesajlaşma
- ✓ gönderildi, ✓✓ iletildi, mavi ✓✓ okundu
- "yazıyor…", çevrimiçi / son görülme
- Okunmamış mesaj rozeti
- Fotoğraf gönderme (otomatik küçültülür), tıklayınca büyür
- Mesajı herkesten silme (mesajın altındaki ⋯ düğmesi)
- Emoji, bağlantılar tıklanabilir
- Tarayıcı bildirimi (uygulama açıkken)
- Ana ekrana yüklenebilir uygulama (PWA): Android'de "Yükle", iPhone'da Paylaş > Ana Ekrana Ekle, bilgisayarda Chrome/Edge adres çubuğundaki yükle simgesi
- Mesaja dokun/⋯ veya uzun bas (bilgisayarda sağ tık): tepki (👍❤️😂😮😢🙏), yanıtla, kopyala, ilet, yıldızla, herkesten sil. Çift dokun = ❤️, sağa kaydır = yanıtla
- Yıldızlı mesajlar listesi, yarım kalan mesaj taslağı kaydı, uzun mesajda "Devamını oku", yeni mesaj sayacıyla "en alta git" düğmesi
- 📎 ile fotoğraf, belge (PDF/Word/Excel/PowerPoint/ZIP, 8 MB) ve konum gönderme; sohbette "Ortak medya ve belgeler" galerisi
- Engelleme ve şikayet, hız sınırları
- Tema: Otomatik / Açık / Koyu; bildirim sesi açma-kapama; titreşim (destekleyen telefonlarda)
- Aramada ekran uyumaz, bağlantı zayıfsa uyarı, ses çubukları
- Başka uygulamadan Selam'a metin/link paylaşma (yüklenmiş uygulamada, Android)

## Bilgisayarın kapalıyken de çalışsın: internete koymak

Adım adım rehber: **YAYINA-ALMA.md** (GitHub + Neon + Render, hepsi ücretsiz). Aynı kod internette `DATABASE_URL` ile Postgres'e, `APPSCRIPT_*` / `BREVO_API_KEY` ile mail gönderimine geçer.

## Önemli: sohbet için herkesin AYNI sunucuya bağlanması gerekir

Bu klasörü herkes kendi bilgisayarında çalıştırırsa her birinin ayrı, boş bir Selam'ı olur ve birbirini bulamaz. Arkadaşlarınla konuşmak için sunucu tek bir yerde çalışmalı. Denemek için:

- Aynı Wi-Fi'de: `.env` dosyasına `HOST=0.0.0.0` ekle, arkadaşların `http://SENIN_IP:3000` adresine girsin (`ipconfig` ile IPv4 adresine bak).
- İnternetten, geçici: `cloudflared tunnel --url http://localhost:3000` verdiği `https://...trycloudflare.com` adresini paylaş. Bilgisayarın ve iki pencere açık kalmalı.

Kalıcı çözüm uygulamayı internetteki bir sunucuya yüklemektir. O zaman her ayarı `.env` yerine barındırma servisinin "ortam değişkenleri" bölümüne gireceksin. SMTP'yi engelleyen servislerde `BREVO_API_KEY` ile Brevo kullan (`.env.example` içinde var). Veriler `DATA_DIR` klasöründe tutulur; barındırma servisinde bu klasör kalıcı disk olmalı.

## Sınırlar

Birebir sohbet ve sesli arama var; görüntülü arama, grup, sesli mesaj, uçtan uca şifreleme yok. Uygulama kapalıyken bildirim (Web Push) yok. Fotoğraflar tahmin edilemez ama herkese açık adreslerde durur. Bildirimler yalnızca uygulama açıkken gelir; kapalıyken bildirim için ayrıca push altyapısı gerekir. Veriler tek bir JSON dosyasında tutulur, çok kullanıcıya geçerken veritabanına taşınmalı.
