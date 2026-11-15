# Selam'ı internete koyma (bilgisayarın kapalıyken de çalışsın)

Hedef: herkesin `https://selam-xxxx.onrender.com` gibi bir adresten girebildiği, senin bilgisayarına bağlı olmayan bir Selam.

Üç ücretsiz hesap gerekiyor ve ikisini senin açman lazım (ben senin adına hesap açamam):

| Ne için | Servis | Neden |
|---|---|---|
| Kodu barındırır | **GitHub** + **Render** | Render siteyi GitHub'daki koddan çalıştırır |
| Mesajları kalıcı saklar | **Neon** (Postgres) | Render'ın diski silinir, Neon'daki veri silinmez |
| Giriş kodunu mail atar | **Gmail API** (aşağıda) | Render ücretsiz planı mail portlarını engeller |

Ücretsiz planın bilinen sınırları (Eylül-Kasım 2026'da kontrol ettiğim kadarıyla, değişebilir):
- **Render:** 15 dakika kimse girmezse uyur, sonraki ilk girişte ~1 dakika açılır. Ayda 750 saat çalışma hakkı var (tek site için yeter). Kullanım dışında hesabına kart tanımlanmazsa aşım olunca site ay sonuna kadar durur.
- **Neon:** 1 GB veri. Fotoğraflar da burada durur (her biri yaklaşık 200 KB), yani birkaç bin fotoğraf.
- **Gmail:** Günde yaklaşık 500 mail. Giriş kodu için fazlasıyla yeter.

---

## 1) Kodu GitHub'a yükle (5 dk)

1. https://github.com adresinde hesap aç, sağ üstte **+ > New repository**. Adı `selam`, **Private** seç, **Create repository**.
2. Açılan sayfada **uploading an existing file** bağlantısına tıkla.
3. Zip'ten çıkan klasörün **içindeki her şeyi** (`server.py`, `public` klasörü, `requirements.txt`, `render.yaml` ...) sürükle bırak. `.env` dosyan varsa **yükleme**, içinde şifren var.
4. **Commit changes** de.

## 2) Veritabanı: Neon (3 dk)

1. https://neon.com adresinde hesap aç, **Create project** de (adı `selam`).
2. Proje sayfasında **Connect** (Connection string) bölümünü aç, `postgresql://...` ile başlayan adresi kopyala. Bunu `DATABASE_URL` olarak kullanacaksın. Şifre içerir, kimseyle paylaşma.

## 3) Mail: Gmail API ile ücretsiz (15 dk, bir kez)

Render ücretsiz planı `smtp.gmail.com` portlarını (25, 465, 587) engellediği için normal Gmail şifresiyle olmaz. Gmail'in web (HTTPS) arayüzünü kullanıyoruz. Google ekranlarının yazıları zamanla değişebilir, aşağıdakiler yaklaşık adımlardır.

1. https://console.cloud.google.com adresinde Gmail hesabınla gir, yeni bir **proje** oluştur (adı `selam`).
2. **APIs & Services > Library** içinde **Gmail API**'yi bul, **Enable** de.
3. **OAuth consent screen** (Google Auth Platform) bölümünü doldur: uygulama adı `Selam`, destek e-postası kendi adresin, kullanıcı türü **External**. **Data Access / Scopes** kısmına `https://www.googleapis.com/auth/gmail.send` ekle.
4. Aynı bölümde yayın durumunu **In production** (Yayına al / Publish app) yap. **Bunu atlama:** "Testing"de kalırsa anahtarın 7 günde ölür ve girişler tekrar mail atamaz olur.
5. **Credentials > Create credentials > OAuth client ID**, türü **Web application**. **Authorized redirect URIs** kısmına tam olarak şunu ekle: `https://developers.google.com/oauthplayground`. Oluşunca **Client ID** ve **Client secret**'ı kopyala.
6. https://developers.google.com/oauthplayground adresine git. Sağ üstteki **dişli** simgesine tıkla, **Use your own OAuth credentials** kutusunu işaretle, Client ID ve secret'ı yapıştır.
7. Sol taraftaki kutuya `https://www.googleapis.com/auth/gmail.send` yaz, **Authorize APIs**'e bas, Gmail hesabını seç. "Google bu uygulamayı doğrulamadı" uyarısı çıkarsa **Advanced > Go to Selam (unsafe)** de (uygulama senin kendi uygulaman).
8. **Exchange authorization code for tokens**'a bas. Çıkan **Refresh token**'ı kopyala.

Elinde şunlar olmalı: Client ID, Client secret, Refresh token ve Gmail adresin.

## 4) Render'da yayına al (5 dk)

1. https://render.com adresinde **GitHub ile** hesap aç.
2. **New > Blueprint** de, `selam` deponu seç. Render `render.yaml` dosyasını okuyup değerleri sorar:
   - `DATABASE_URL`: Neon'dan kopyaladığın adres
   - `GMAIL_CLIENT_ID`, `GMAIL_CLIENT_SECRET`, `GMAIL_REFRESH_TOKEN`: 3. adımdaki değerler
   - `MAIL_FROM`: `Selam <senin.adresin@gmail.com>`
3. **Apply / Deploy** de. İlk kurulum birkaç dakika sürer.
4. Bitince Render sana `https://selam-xxxx.onrender.com` gibi bir adres verir. Aç, e-postanla giriş yapmayı dene. Kod gerçekten mailine gelmeli.

Blueprint seçeneği yerine elle kurmak istersen: **New > Web Service**, depoyu seç, Runtime **Python**, Build Command `pip install -r requirements.txt`, Start Command `python server.py`, Instance Type **Free**, ve yukarıdaki değerleri **Environment** bölümüne gir.

Sorun olursa Render'da servisin **Logs** sekmesine bak. Selam açılışta ne olduğunu yazar ("E-posta: Gmail API", "Veriler: Postgres" satırlarını görmelisin). Mail hatası olursa nedenini Türkçe yazar.

## 5) Kullanıma başla

- Adresi arkadaşlarına gönder. Uygulamadaki **🔗 Davet** düğmesi sana özel bir link verir (`.../?u=kullaniciadin`): arkadaşın linke tıklayıp giriş yapınca doğrudan seninle sohbet açılır.
- **Ana ekrana ekle:** Android'de Chrome'da "Yükle", iPhone'da Safari'de Paylaş > Ana Ekrana Ekle, bilgisayarda Chrome/Edge adres çubuğundaki yükle simgesi.
- **Uyumayı azaltmak (isteğe bağlı):** https://uptimerobot.com gibi ücretsiz bir servisle `https://selam-xxxx.onrender.com/healthz` adresini 5 dakikada bir çağırtırsan site uyumaz. Bu, aylık 750 saatin neredeyse hepsini kullanır, o yüzden Render'da başka ücretsiz site açma.

## Mail için diğer yollar

- **Kendi alan adın varsa (yılda birkaç dolar):** Brevo'nun ücretsiz planı + doğrulanmış alan adı. `BREVO_API_KEY` ve `MAIL_FROM=Selam <noreply@senin-alanadin.com>` ver. Brevo, Gmail gibi ücretsiz adresleri gönderen olarak kabul etmez, kendi adresiyle değiştirip spam'e düşürür, o yüzden alan adı şart.
- **Ücretli Render (yaklaşık ayda 7 dolar):** SMTP portları açık olur, normal Gmail uygulama şifresiyle (`SMTP_HOST=smtp.gmail.com`, `SMTP_USER`, `SMTP_PASS`) çalışır, ayrıca kalıcı disk eklenebilir.

## Sesli arama ve TURN

Sesli arama ses akışını kullanıcılar arasında doğrudan kurar, sunucu sadece bağlantıyı tanıştırır. Çoğu ağda ücretsiz Google STUN sunucusu yeter. Bazı mobil operatör ve şirket ağlarında doğrudan bağlantı kurulamaz, o zaman "Bağlantı kurulamadı" der. Çözüm bir **TURN** sunucusudur (ses oradan aktarılır). Ücretsiz katmanı olan bir TURN servisinden aldığın bilgileri Render'da Environment'a gir: `TURN_URLS` (virgülle ayrılmış, örn. `turn:ornek.com:3478,turns:ornek.com:5349`), `TURN_USERNAME`, `TURN_CREDENTIAL`. Servis sana `iceServers` listesi veriyorsa onu olduğu gibi `ICE_SERVERS_JSON` olarak da girebilirsin. Ben bir TURN servisiyle gerçek ağda deneyemedim, bu yüzden bu kısım denenmemiş.

## Bilmen gerekenler

- Bildirimler yalnızca uygulama açıkken gelir. Kapalıyken bildirim için ayrıca push altyapısı gerekir.
- Android paketi (apk) ve Windows paketi: site yayına girdikten sonra https://www.pwabuilder.com'a adresini yazıp ücretsiz oluşturabilirsin. iPhone için "Ana Ekrana Ekle" yeterli, App Store için ücretli Apple geliştirici hesabı gerekir.
- Herkese açık bir sitede herkes kayıt olabilir. Fotoğraflar tahmin edilemez adreslerde durur ama linki bilen görebilir.
