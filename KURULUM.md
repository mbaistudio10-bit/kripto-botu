# Kripto Botu: Kurulum ve Kullanım Rehberi (Faz 4)

> **Önemli:** Bu yazılım yatırım tavsiyesi değildir. Geçmiş performans gelecekteki getiriyi garanti etmez.
> Bot şu an **gerçek emir göndermez**. Bir trend stratejisiyle sahte parayla (10.000 TL) deneme işlemleri yapar, riskleri izler ve size Telegram'dan (Achilles) haber verir.

Kodlama bilgisi gerekmez. Python kurmanıza da gerek yok.

---

## 1. Programı indirin

1. GitHub'daki proje sayfasında sağdaki **Releases → "KriptoBotu (en son sürüm)"** bağlantısına tıklayın.
2. Windows için **KriptoBotu-Windows.zip**, Mac için **KriptoBotu-Mac.zip** dosyasını indirin.
3. Zip'e sağ tıklayıp **"Tümünü ayıkla"** deyin. Çıkan `KriptoBotu` klasörünü Masaüstüne koyun.

**Güncelleme yaparken:** yeni zip'i aynı klasörün üstüne açabilirsiniz. `.env` dosyanız ve `data` klasörünüz silinmez.
"Dosyaların yerine geçsin mi?" diye sorarsa **"Evet"** deyin (yeni `config/settings.yaml` gerekli).

---

## 2. Botu başlatın

`KriptoBotu` klasöründeki **KriptoBotu** dosyasına çift tıklayın.
- "Windows kişisel bilgisayarınızı korudu" çıkarsa: **"Ek bilgi" → "Yine de çalıştır"**.

Siyah pencerede önce çiftler kontrol edilir, sonra her 5 saniyede bir fiyat ve risk tablosu çıkar.
**Durdurmak için:** pencere seçiliyken **Ctrl + C**.

İlk çalıştırmada klasörde **`.env`** adlı bir dosya oluşur. Bu sizin gizli ayar dosyanızdır.

---

## 3. Telegram'ı (Achilles) bağlayın

### 3a. Anahtarı yenileyin (önemli, bir kez)
Bot anahtarınız sohbette paylaşıldığı için artık güvenli değil. Yenisini alın:
1. Telegram'da **@BotFather**'a gidin ve **`/revoke`** yazın.
2. **Achilles** botunu seçin. BotFather size **yeni bir anahtar** verir (eski anahtar artık çalışmaz).
3. Bu yeni anahtarı **kimseyle paylaşmayın**, sohbete de yapıştırmayın. Sadece aşağıdaki dosyaya yazın.

### 3b. `.env` dosyasını doldurun
1. Botu kapatın (Ctrl + C).
2. `KriptoBotu` klasöründeki **`.env`** dosyasına sağ tıklayın → **"Birlikte aç" → "Not Defteri"**.
3. Şu iki satırı doldurun (eşittirin sağına, boşluk bırakmadan):
   ```
   TELEGRAM_BOT_TOKEN=BotFather'ın verdiği yeni anahtar
   TELEGRAM_CHAT_ID=Telegram kimlik numaranız
   ```
   Kimlik numaranızı bilmiyorsanız bu satırı boş bırakın, botu başlatın ve Achilles'e Telegram'dan bir mesaj yazın:
   Achilles size numaranızı söyler. Onu buraya yazıp botu yeniden başlatın.
4. Kaydedin (Ctrl + S) ve botu yeniden başlatın.

Telefonunuza **"✅ Achilles: Kripto Botu başladı"** mesajı gelirse her şey tamam.

> Mac'te `.env` dosyası gizlidir. Klasördeyken **Cmd + Shift + .** tuşlarına basınca görünür.

### 3c. Telegram komutları
| Komut | Ne yapar |
|---|---|
| `/durum` | Botun anlık durumu, fiyatlar, bakiye |
| `/limitler` | Risk limitleri ve ne kadarının kullanıldığı |
| `/rapor` | Bugünkü kazanç özeti (`/rapor hafta`, `/rapor ay`) |
| `/durdur` | Yeni işlem açmayı durdurur. Bot izlemeye devam eder |
| `/devam` | Durdurmayı kaldırır |
| `/onayla` | Ayar dosyasında gevşettiğiniz limitleri onaylar |
| `/yardim` | Komut listesi |

Bot sadece **sizin** Telegram hesabınızdan gelen komutlara cevap verir. Başka kişilerin ve başka botların mesajlarını yok sayar.

### 3d. Size ne zaman mesaj gelir?
- 🔴 **KRİTİK:** günlük/haftalık zarar limiti doldu, acil durdurma, bot çöktü, açık pozisyonu olan çift borsadan kalktı
- 🟠 **UYARI:** limite yaklaşılıyor, kur eski, bir çiftin likiditesi yetersiz, sistem hatası, biri botu durdurdu
- 🛒 **Alış**, 💰 **Satış**, 🛑 **Stop-loss**: her deneme işleminde anında (alışta "Sebep" satırı stratejinin neden aldığını yazar)
- 🔒 **Stop yükseltildi**: bir işlem kâra geçip stop alış fiyatının üstüne çekilince
- Aynı uyarı 30 dakikada bir defadan fazla gelmez. KRİTİK ve işlem mesajları her zaman gelir.

---

## 4. Risk kuralları (bot bunları kendi uygular)

| Kural | Ne olur |
|---|---|
| Günlük zarar %3 | O gün yeni işlem açılmaz. Gece 00:00'da (TR) kendiliğinden açılır. Elle kaldırılamaz. |
| Haftalık zarar %6 | Pazartesiye kadar yeni işlem açılmaz. Elle kaldırılamaz. |
| Zirveden düşüş %10 | **Acil durdurma**: yeni işlem yok, açık pozisyonlar kapatılır. Kaldırmak için `/devam ONAY`. |
| İşlem başına risk %1 | Stop fiyatına kadar olası zarar bakiyenin %1'ini geçemez. |
| En fazla 2 pozisyon | Aynı anda en fazla 2 açık işlem. |
| Kur 5 dakikadan eski | Yeni işlem açılmaz. |
| Likidite yetersiz | Alış-satış farkı geniş, hacim düşük veya emir defteri sığ olan çiftte işlem açılmaz. |

Bot kapatılıp açılsa bile durdurmalar hatırlanır (`data/risk_state.json`).

Limitleri `config/settings.yaml` dosyasından değiştirebilirsiniz. **Sıkılaştırmak** hemen geçerli olur.
**Gevşetmek** (örneğin %3'ü %5 yapmak) ancak Telegram'dan `/onayla` yazınca geçerli olur.

---

## 5. Jarvis bağlantısı

Bot ilk açılışta `.env` dosyasına Jarvis için iki anahtar yazar:
- `JARVIS_READ_KEY`: sadece okuma (durum, rapor, olaylar)
- `JARVIS_CONTROL_KEY`: acil durumda **durdurma ve devam ettirme**

Bu anahtarları sadece Jarvis'e verin. Jarvis'in yapabildikleri:
- Durumu, kazanç raporunu ve olayları okumak ("X raporu" için)
- Acil durumda yeni işlemleri durdurmak, kendi durdurmasını kaldırmak

Jarvis'in **yapamadıkları:** emir göndermek, limit değiştirmek, sizin yaptığınız durdurmayı veya zarar limiti/acil durdurmayı kaldırmak.
Jarvis botu her durdurduğunda size Telegram'dan haber gelir.

Teknik ayrıntılar (Jarvis'i kuran kişi için): `README.md` → "Jarvis API".

---

## 6. Strateji: ne zaman alır, ne zaman satar? (Faz 4)

Bot **sahte parayla** çalışan bir deneme hesabı tutar (başlangıç: 10.000 TL). Borsaya hiçbir emir gitmez.
Alış ve satışlar OKX'teki gerçek fiyat ve emir defterine göre, komisyon (%0,10) dahil hesaplanır.
Faz 3'teki "deneme tetikleyicisi" kaldırıldı; yerine basit bir **trend takip stratejisi** geldi.
Geçmiş veri testinde en istikrarlı çıkan **4 saatlik mum + günlük trend** ayarı kullanılıyor (ilk ayar 1 saatlik mumdu).

**Ne zaman alır?** (BTC/TRY ve ETH/TRY, her 4 saatlik mum kapanınca bakar: Türkiye saatiyle 03:00, 07:00, 11:00, 15:00, 19:00, 23:00) Hepsi aynı anda doğruysa:
1. **Büyük resim yukarı:** günlük mumlarda fiyat ortalamasının üstünde ve kısa ortalama uzun ortalamanın üstünde.
2. Fiyat 4 saatlik ortalamasının üstünde.
3. **Kırılım:** fiyat son 20 mumun (yaklaşık 3 günün) en yüksek seviyesinin üstünde kapandı.
4. Fiyat ne çok durgun ne çok çılgın oynuyor.

Sonra **risk motoru** son sözü söyler (işlem başına en fazla %1 risk, en fazla 2 pozisyon, kur ve likidite kontrolü).
Risk kuralı izin vermezse alış yapmaz ve size nedenini yazar (en fazla 30 dakikada bir).
**Günlerce, hatta haftalarca alış olmaması normaldir.** Strateji net bir yükseliş bekler.

**Ne zaman satar?** (alıştan bağımsız ayrı kurallar)
- **Zarar-kes (stop):** alışta, fiyatın normal oynama payının 2,5 katı aşağıya konur. Fiyat oraya inerse satar.
- **Başa-baş:** fiyat, stop mesafesi kadar yükselince stop alış fiyatının biraz üstüne çekilir (🔒 mesajı gelir).
- **İz süren stop:** sonra fiyat yükseldikçe stop da yukarı gelir, hiç aşağı inmez. Böylece kârın bir kısmı korunur.
- **Süre:** 3 günde kâra geçemeyen işlem satılır; her işlem en geç 15 gün sonra satılır.
- **Trend bozulursa:** günlük yükseliş trendi biterse satar.

**Ne görürsünüz?**
- Siyah pencerede ve `/durum`'da her çift için stratejinin son kararı ("bekle, ..." veya "ALIŞ sinyali").
- Telegram'a 🛒 ALIŞ (deneme), 💰 SATIŞ (deneme), 🛑 STOP-LOSS, 🔒 stop yükseltildi mesajları; kazanç hem TL hem USD olarak.
- `/durdur`: yeni alışları durdurur. **Açık pozisyonların stop ve satış kuralları çalışmaya devam eder.**
- Zirveden %10 düşüş olursa bot tüm deneme pozisyonlarını kendisi kapatır.

USD kazancı, alış ile satış arasındaki dolar kuru değişimini de içerir. Bu yüzden TL'de kâr, USD'de daha az görünebilir.
Hesap bot kapatılıp açılınca kaldığı yerden devam eder (`data/paper_state.json`).
Deneme hesabını sıfırdan başlatmak isterseniz: botu kapatın, bu dosyayı silin, botu açın.
Stratejiyi kapatmak için `config/settings.yaml` içinde `strategy` → `enabled: false`.

---

## 7. Geçmiş veri testi (backtest)

Stratejinin geçmişte nasıl sonuç verirdi, bunu görmek için:
- **Windows:** `KriptoBotu` klasöründeki **GeriyeTest.bat** dosyasına çift tıklayın.
- **Mac:** **GeriyeTest.command** dosyasına çift tıklayın.

Bot OKX'ten son 2 yılın saatlik fiyatlarını indirir (ilk seferde birkaç dakika, sonra hızlı), stratejiyi aynı kurallarla,
komisyon ve kayma (kötü fiyat) payı dahil dener ve sonucu ekrana yazar. Telegram bağlıysa rapor telefonunuza da gelir.
Rapor ayrıca `data/backtest_report.txt` dosyasına kaydedilir. Bu test sırasında ana botun kapalı olmasına gerek yok.

Test **3 kuralı yan yana** dener. Hepsi aynı fikir (trend + kırılım), sadece hızları farklı:
1. Şu anki ayarlar (4 saatlik mum, günlük trend)
2. 1 saatlik mum, 4 saatlik trend (ilk ayarlar, daha hızlı)
3. Günlük mum, günlük trend (en yavaş)

Her kural iki kez denenir:
- **TL çiftleriyle** (BTC/TRY, ETH/TRY): boştaki para TL olarak durur.
- **Dolar çiftleriyle** (BTC/USDT, ETH/USDT): aynı 10.000 TL başta dolara (USDT) çevrilir, boştaki para dolar olarak durur.
  Sonuç yine hem TL hem USD olarak yazılır. Dolar çiftlerinde bir kural ancak **dolar olarak da** kâr ettiyse "kârlı" sayılır.
  (Deneme botu şimdilik TL çiftleriyle çalışmaya devam eder; dolar çiftleri sadece bu testte kullanılır.)

Bu ayarlar önceden sabitlendi; sonuca bakıp değiştirilmedi. Her kural için iki satır vardır:
- **Öğrenme dönemi** (ilk %70)
- **Sınama dönemi** (son %30): kuralların hiç görmediği dönem. Asıl önemli olan budur.

Acil durdurma (zirveden %10 düşüş) olursa test 7 gün ara verir, sonra sizin "/devam ONAY" yazdığınızı varsayıp devam eder.
Böylece her dönemin tamamı ölçülür. Kaç kez acil durdurma olduğu raporda yazar.
"Sadece dolar (USDT) tutmak" satırı, paranızı hiç işlem yapmadan dolarda tutsaydınız TL bazında ne olurdu, onu gösterir.
Tüm ayrıntılar `data/backtest_report.txt` dosyasındadır.

"Alıp hiç satmamak" satırı, aynı dönemde sadece alıp beklemenin sonucunu gösterir (karşılaştırma için).

> **Unutmayın:** geçmiş veri testi yatırım tavsiyesi değildir. Geçmişte işe yarayan bir kural gelecekte para kaybettirebilir.
> Geçmiş performans gelecekteki getiriyi garanti etmez. TRY çiftlerinde işlem az olduğu için gerçek fiyatlar testtekinden kötü olabilir.

---

## 8. Dosyalar nerede?
- `.env`: gizli anahtarlar (kimseyle paylaşmayın)
- `logs/bot.log`: teknik kayıtlar (bir sorun olursa bunu bana gönderin, `.env`'yi değil)
- `data/events.jsonl`: tüm uyarı ve olayların kaydı
- `data/risk_state.json`: durdurmaların ve limitlerin hafızası
- `data/status.json`: botun anlık durumu
- `data/paper_state.json`: deneme hesabının bakiyesi ve açık pozisyonları
- `data/candles/`: indirilen geçmiş fiyatlar (silerseniz yeniden indirilir)
- `data/backtest_report.txt`: son geçmiş veri testinin raporu
