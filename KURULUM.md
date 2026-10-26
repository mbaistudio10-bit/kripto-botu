# Kripto Botu: Kurulum ve Kullanım Rehberi (Faz 3)

> **Önemli:** Bu yazılım yatırım tavsiyesi değildir. Geçmiş performans gelecekteki getiriyi garanti etmez.
> Bot şu an **gerçek emir göndermez**. Sahte parayla (10.000 TL) deneme işlemleri yapar, riskleri izler ve size Telegram'dan (Achilles) haber verir.

Kodlama bilgisi gerekmez. Python kurmanıza da gerek yok.

---

## 1. Programı indirin

1. GitHub'daki proje sayfasında sağdaki **Releases → "KriptoBotu (en son sürüm)"** bağlantısına tıklayın.
2. Windows için **KriptoBotu-Windows.zip**, Mac için **KriptoBotu-Mac.zip** dosyasını indirin.
3. Zip'e sağ tıklayıp **"Tümünü ayıkla"** deyin. Çıkan `KriptoBotu` klasörünü Masaüstüne koyun.

**Güncelleme yaparken:** yeni zip'i aynı klasörün üstüne açabilirsiniz. `.env` dosyanız ve `data` klasörünüz silinmez.

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
- 🛒 **Alış**, 💰 **Satış**, 🛑 **Stop-loss**: her deneme işleminde anında
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

## 6. Deneme işlemleri (Faz 3)

Bot **sahte parayla** çalışan bir deneme hesabı tutar (başlangıç: 10.000 TL). Borsaya hiçbir emir gitmez.
Alış ve satışlar OKX'teki gerçek fiyat ve emir defterine göre, komisyon (%0,10) dahil hesaplanır.

**Ne zaman alış yapar?** Şimdilik bir **deneme tetikleyicisi** var. Bu bir strateji **değildir**, kazanç beklentisi yoktur.
Tek amacı, sistemin işlem yaptığını, mesajları ve risk kurallarını görebilmenizdir:
- Bot açıldıktan 3 dakika sonra, sonra her 30 dakikada bir, BTC/TRY ve ETH/TRY arasında sırayla küçük bir alış dener.
- Her alış, risk motorunun onayından geçer (işlem başına en fazla %1 risk, en fazla 2 pozisyon, kur ve likidite kontrolü).
- Fiyat **%1 düşerse** stop-loss ile satar, **%1,5 yükselirse** kârla satar, **3 saat** dolarsa satar.
- Risk kuralı izin vermezse (örneğin emir defteri sığ) alış yapmaz ve size nedenini yazar (en fazla 30 dakikada bir).

Gerçek strateji Faz 4'te gelecek. Deneme tetikleyicisini kapatmak için `config/settings.yaml` içinde `test_trigger` → `enabled: false`.

**Ne görürsünüz?**
- Telegram'a 🛒 ALIŞ (deneme), 💰 SATIŞ (deneme), 🛑 STOP-LOSS mesajları; kazanç hem TL hem USD olarak.
- `/durum`: deneme hesabının değeri (TL ve USD), açık pozisyonlar, anlık kâr/zarar.
- `/rapor`: kapanan işlemlerin özeti.
- `/durdur`: yeni alışları durdurur. **Açık pozisyonların stop-loss ve hedefi çalışmaya devam eder.**
- Zirveden %10 düşüş olursa bot tüm deneme pozisyonlarını kendisi kapatır.

USD kazancı, alış ile satış arasındaki dolar kuru değişimini de içerir. Bu yüzden TL'de kâr, USD'de daha az görünebilir.

Hesap bot kapatılıp açılınca kaldığı yerden devam eder (`data/paper_state.json`).
Deneme hesabını sıfırdan başlatmak isterseniz: botu kapatın, bu dosyayı silin, botu açın.

---

## 7. Dosyalar nerede?
- `.env`: gizli anahtarlar (kimseyle paylaşmayın)
- `logs/bot.log`: teknik kayıtlar (bir sorun olursa bunu bana gönderin, `.env`'yi değil)
- `data/events.jsonl`: tüm uyarı ve olayların kaydı
- `data/risk_state.json`: durdurmaların ve limitlerin hafızası
- `data/status.json`: botun anlık durumu
- `data/paper_state.json`: deneme hesabının bakiyesi ve açık pozisyonları
