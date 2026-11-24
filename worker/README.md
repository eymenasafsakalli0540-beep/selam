# Selam – Cloudflare Worker (kartsız)

Canlı site: https://selam-eymen.eymenasafsakalli0540.workers.dev

Ücretsiz Cloudflare Workers + KV + Durable Objects kullanır. Kredi kartı gerekmez; sunucu uyumaz/durmaz.

## Yerel kurulum

```powershell
npm install
npx wrangler login
npx wrangler secret put APPSCRIPT_URL
npx wrangler secret put APPSCRIPT_SECRET
npx wrangler deploy
```

> `APPSCRIPT_SECRET` değerini UTF-8 olarak yükleyin; aksi halde Türkçe karakterler bozulur ve mail `yetkisiz` döner.

Kaynak: `src/index.js`, statik dosyalar: `public/`.
