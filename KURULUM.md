# Kripto Botu: Kurulum ve Çalıştırma Rehberi (Faz 1)

> **Önemli:** Bu yazılım yatırım tavsiyesi değildir. Geçmiş performans gelecekteki getiriyi garanti etmez.
> Faz 1'deki bot **sadece fiyatları okur**. Hiçbir emir göndermez, hesabınıza erişmez ve API anahtarı istemez.

Bu rehber kodlama bilgisi gerektirmez. Adımları sırayla yapmanız yeterli.

---

## 1. Python'u kurun (bir kez yapılır)

### Windows
1. https://www.python.org/downloads/ adresine gidin.
2. Sarı **"Download Python 3.x"** düğmesine basın (3.11 veya daha yeni olmalı).
3. İndirilen dosyayı açın. **En alttaki "Add python.exe to PATH" kutusunu mutlaka işaretleyin.**
4. **"Install Now"** düğmesine basın ve kurulum bitince pencereyi kapatın.

### Mac
1. https://www.python.org/downloads/ adresinden **macOS** sürümünü indirin (3.11 veya daha yeni).
2. İndirilen `.pkg` dosyasını açıp "Devam" diyerek kurun.

---

## 2. Bot dosyalarını yerleştirin

1. Size gönderilen `kripto-botu.zip` dosyasını indirin.
2. Dosyaya sağ tıklayıp **"Tümünü ayıkla"** (Mac'te çift tıklama) ile açın.
3. Çıkan `kripto-botu` klasörünü kolay bulacağınız bir yere koyun (örneğin Masaüstü).

---

## 3. Kurulumu çalıştırın (bir kez yapılır)

- **Windows:** `kripto-botu` klasöründeki **`kurulum_windows.bat`** dosyasına çift tıklayın.
  - "Windows kişisel bilgisayarınızı korudu" uyarısı çıkarsa **"Ek bilgi" → "Yine de çalıştır"** deyin.
- **Mac:** **`kurulum_mac.command`** dosyasına sağ tıklayıp **"Aç"** deyin (ilk seferde güvenlik onayı ister).

Siyah bir pencere açılır, gerekli paketler internetten indirilir ve testler çalışır.
Sonunda `22 passed` ve **"Kurulum tamam"** yazısını görmelisiniz. Bir tuşa basıp kapatın.

---

## 4. Botu başlatın

- **Windows:** **`calistir_windows.bat`** dosyasına çift tıklayın.
- **Mac:** **`calistir_mac.command`** dosyasına çift tıklayın.

Önce bot OKX'te çiftlerin olup olmadığını kontrol eder:

```
  [OK]   BTC/TRY borsada var ve işlem görüyor
  [OK]   ETH/TRY borsada var ve işlem görüyor
  [OK]   USDT/TRY borsada var ve işlem görüyor
```

Sonra her 5 saniyede bir şöyle bir tablo yazar:

```
=== Kripto Botu | 2026-10-26T09:00:00+00:00 | mod: PAPER_TRADING | borsa: OKX ===
USD/TRY kuru: 41.9500 (USDT/TRY, güncel)
Çift             Son fiyat         USD   24s %  Spread(bps)  Durum
BTC/TRY       3512345.6000  83727.0000    1.20         2.85  canlı
...
```

**Durdurmak için:** pencere seçiliyken **Ctrl + C** tuşlarına basın.

### Tablodaki kelimelerin anlamı
| Kelime | Anlamı |
|---|---|
| Son fiyat | Son işlem fiyatı (TRY) |
| USD | Aynı fiyatın dolar karşılığı (USDT/TRY kuruyla) |
| 24s % | Son 24 saatteki değişim |
| Spread (bps) | Alış ve satış fiyatı arasındaki fark. 1 bps = %0,01. Küçük olması iyidir. |
| canlı / ESKİ VERİ | Fiyat 30 saniyeden eskiyse "ESKİ VERİ" yazar |
| Kur "ESKİ" | Kur 5 dakikadan eskiyse bot ileride yeni pozisyon açmayacak (güvenlik kuralı) |

---

## 5. Ekranda [YOK] yazarsa

`[YOK] BTC/TRY bu borsa adresinde bulunamadı` görürseniz, TRY çiftleri bu OKX adresinde yok demektir.
Türkiye'deki TRY çiftleri **OKX TR**'de işlem görür. Bu durumda ekranın fotoğrafını bana gönderin, adresi birlikte düzeltelim.

Bot "OKX'e bağlanılamadı" derse: internet bağlantınızı kontrol edin ve tekrar başlatın.

---

## 6. Ayarları değiştirmek

Tüm ayarlar `config/settings.yaml` dosyasındadır. Not Defteri (Mac'te TextEdit) ile açabilirsiniz.
Başında `#` olan satırlar açıklamadır. Bir değeri değiştirip kaydedin ve botu yeniden başlatın.
Yanlış bir şey yazarsanız bot başlamaz ve neyin hatalı olduğunu söyler.

Faz 1'de `LIVE_TRADING` (gerçek para) modu bilerek kapalıdır; seçilirse bot çalışmaz.

---

## 7. Dosyalar nerede?
- `logs/bot.log`: teknik kayıtlar (bir sorun olursa bunu bana gönderin)
- `data/status.json`: botun anlık durumu. Faz 5'te telefondan takip ekranı bu dosyayı okuyacak.
