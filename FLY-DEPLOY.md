# Selam → Fly.io dağıtım rehberi

Bu klasör, GitHub’daki `selam` reposunun kopyası + Fly.io ayarlarıdır.

## 1. Fly.io hesabı

1. https://fly.io/app/sign-up/ → **Sign up with GitHub**.
2. GitHub izni onayla.
3. Dashboard → **Billing** → kart ekle. (Kart bilgisi burada kalır, koda girmez.)
4. Aylık $5 kullanım hakkı içinde bu uygulama yaklaşık **$2.2/ay** tutar ve **uyumaz**.

## 2. Gizli bilgileri yerel .env dosyasına yaz

Bu klasörde bir `.env` dosyası oluştur (Not Defteri ile), şu üç satırı doldur:

```
DATABASE_URL=postgresql://...neon bağlantı adresi...
APPSCRIPT_URL=https://script.google.com/macros/s/..../exec
APPSCRIPT_SECRET=scriptteki-uzun-rastgele-yazi
```

`.env` dosyası `.dockerignore` ve `.gitignore` sayesinde ne Docker imajına ne de GitHub’a girer. Şifreleri bana göndermene gerek yok; dosyayı buraya koyduktan sonra ben okutup Fly’a tanımlayacağım.

## 3. Dağıtım (ben çalıştıracağım)

```powershell
$fly = "$env:USERPROFILE\.fly\bin\flyctl.exe"
cd "C:\Users\muham\OneDrive\Belgeler\Varsayılan Proje\selam-fly\selam-main"

& $fly auth login
& $fly apps create selam-eymen-0540
Get-Content .env | ForEach-Object {
  if ($_ -match '^([^=#]+)=(.*)$') { $vars["$($Matches[1])"] = $Matches[2] }
}
# (gerçek komut dosyada şifreleri ekrana basmadan çalıştırılır)
& $fly secrets set DATABASE_URL="..." APPSCRIPT_URL="..." APPSCRIPT_SECRET="..."
& $fly deploy --ha=false
& $fly open
```

## 4. Yayın adresi

Dağıtımdan sonra adres şu olur:

```
https://selam-eymen-0540.fly.dev
```

Arkadaşlarına bu adresi gönder. Davet linkleri `.../?u=kullaniciadin` şeklinde çalışmaya devam eder.

## 5. Kod değişikliğinde yeniden yayınlama

```powershell
cd "C:\Users\muham\OneDrive\Belgeler\Varsayılan Proje\selam-fly\selam-main"
& "$env:USERPROFILE\.fly\bin\flyctl.exe" deploy
```

İstersen sonradan GitHub Actions ile otomatik dağıtım da ekleyebiliriz; o zaman Fly API token gerekir. Şimdilik token yok, manuel deploy var — “token süresi doldu” sorunu da yok.

## Sorun giderme

```powershell
& "$env:USERPROFILE\.fly\bin\flyctl.exe" logs
& "$env:USERPROFILE\.fly\bin\flyctl.exe" status
```

Açılışta şu satırları görmelisin:

- `E-posta: Google Apps Script ...`
- `Veriler: Postgres veritabanı (kalıcı)`

Bunlar görünmüyorsa gizli bilgiler eksiktir: `fly secrets list` ile kontrol et.
