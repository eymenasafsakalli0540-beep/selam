// Selam için mail gönderici. script.google.com adresinde yeni proje açıp bunu yapıştır.
const SECRET = 'BURAYA-UZUN-RASTGELE-BIR-YAZI-YAZ';  // Render'daki APPSCRIPT_SECRET ile AYNI olmalı

function doPost(e) {
  try {
    const d = JSON.parse(e.postData.contents);
    if (d.secret !== SECRET) return out({ ok: false, error: 'yetkisiz' });
    if (!/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(String(d.to))) return out({ ok: false, error: 'adres' });
    MailApp.sendEmail({
      to: d.to,
      subject: String(d.subject).slice(0, 200),
      body: String(d.body).slice(0, 2000),
      name: String(d.name || 'Selam').slice(0, 50),
    });
    return out({ ok: true });
  } catch (err) {
    return out({ ok: false, error: String(err) });
  }
}

function out(o) {
  return ContentService.createTextOutput(JSON.stringify(o)).setMimeType(ContentService.MimeType.JSON);
}
