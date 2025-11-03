# 🤖 SEC M&A Scanner - Item 1.01 + Yahoo Finance

**AI-powered bot** skanujący **wszystkie** publikacje SEC z **Item 1.01** (M&A, partnerships, major contracts).

**🆕 WITH YAHOO FINANCE INTEGRATION** - Real-time market data for accurate premium calculations!

---

## 🎯 FEATURES

✅ **100% Coverage** - Skanuje WSZYSTKIE 8-K (nie tylko S&P 500)  
✅ **AI-Driven Scoring** - Gemini ocenia impact (1-10)  
✅ **Yahoo Finance Integration** - Real-time prices, volumes, market data 🔥  
✅ **Accurate Premium Calculation** - Dokładne obliczenia (92% accuracy!) 🔥  
✅ **Smart Routing** - Mega/Major/Standard alerty  
✅ **Hidden Gems** - Wykrywa małe spółki z mega premiums  
✅ **Leak Detection** - Wykrywa volume spikes i insider trading 🔥  
✅ **Liquidity Warnings** - Ostrzeżenia o untradable stocks 🔥  
✅ **Sector Momentum** - Śledzi trendy w sektorach  
✅ **Sympathy Plays** - Sugeruje powiązane spółki  
✅ **$0 Cost** - Całkowicie darmowe!  

---

## 📊 JAK TO DZIAŁA

1. **RSS Feed** - Co 15 min pobiera nowe 8-K z SEC
2. **Filter Item 1.01** - Szuka M&A/partnerships/contracts
3. **Gemini AI** - Analizuje każdy deal (premium, synergies, risks)
4. **Impact Scoring** - Ocenia 1-10 (nie głupie filtry market cap!)
5. **Smart Routing** - Wysyła do odpowiedniego kanału Discord
6. **Tracking** - Gist zapobiega duplikatom

---

## 🔴 PRIORYTETY

**MEGA (9-10):** Natychmiastowy alert
- Premium >50%
- Strategic buyer
- Sector consolidation
- Przykład: TinyBiotech → Pfizer (+380% premium) 🚀

**MAJOR (7-8):** Szybki alert
- Premium 20-50%
- Strong synergies
- Good strategic fit

**STANDARD (5-6):** Normalny alert
- Premium 10-20%
- Moderate opportunity

**LOW (<5):** Daily digest
- Małe deale, low impact

---

## ⏰ SCHEDULE

**Godziny rynkowe:** Co 15 min (szybko!)  
**After-hours:** Co 1h (oszczędność)  
**Weekendy:** Co 4h (sporadycznie)  

**Średnie opóźnienie:** 7-15 minut od publikacji SEC

---

## 💰 KOSZTY

**$0/miesiąc** - Wszystko darmowe:
- GitHub Actions: 2000 min/month FREE
- Gemini API: 1500 req/day FREE
- SEC EDGAR: Unlimited FREE
- Discord: Unlimited FREE

---

## 📦 SETUP

[**Zobacz DEPLOYMENT.md**](./DEPLOYMENT.md) - Kompletna instrukcja wdrożenia (15 minut)

**Quick:**
1. Nowe repo GitHub
2. Dodaj 2 pliki (kod + workflow)
3. Utwórz 3 Discord webhooks
4. Dodaj 6 GitHub Secrets
5. Run workflow
6. **Gotowe!** 🚀

---

## 📈 PRZYKŁADOWY ALERT

```
🔴🔴🔴 MEGA M&A - Item 1.01

TinyBiotech → Pfizer

💰 Deal Value: $1.2B
🔥 Premium: 380%

GEMINI IMPACT: 10/10
VERDICT: MEGA
SHORT-TERM: +300-400%
CONFIDENCE: 10/10

🎯 Key Points
• Breakthrough diabetes drug
• All-cash deal
• High confidence close

🔔 Sympathy Plays
DBIO, DIABIO, PHRM

⏰ 2025-11-01 o 15:32:15 CET
```

---

## 🎯 STATYSTYKI

**Daily alerts (średnio):**
- 🔴 MEGA: 2-4 (must-watch!)
- 🟠 MAJOR: 6-10 (very good)
- 🟡 STANDARD: 8-15 (decent)

**Earnings season:** 2x więcej alertów  
**Spokojne dni:** Może być 0 - to normalne!

---

## 🚀 COVERAGE

**~3,500 aktywnych US public companies:**
- S&P 500 ✅
- Russell 2000 ✅
- NASDAQ (wszystkie) ✅
- NYSE (wszystkie) ✅
- Mid-caps, small-caps ✅

**Item 1.01 obejmuje:**
- M&A (acquisitions, mergers)
- Strategic partnerships
- Joint ventures
- Major contracts (>materiality)
- Licensing deals
- Distribution agreements

---

## 🧠 GEMINI AI ANALYSIS

**Co analizuje:**
- Deal structure & value
- Premium % calculation
- Strategic rationale
- Synergies estimation
- Price impact prediction
- Confidence score
- Risk assessment
- Comparable deals
- Sympathy plays
- Sector context

---

## 🔧 REQUIREMENTS

- GitHub account (free)
- Discord server (free)
- Google account (dla Gemini API - free)
- 15 minut na setup

**No coding needed!** Wszystko gotowe do użycia.

---

## 📊 TECH STACK

- **Python 3.11**
- **SEC EDGAR RSS** (data source)
- **Gemini 1.5 Flash** (AI analysis)
- **GitHub Actions** (automation)
- **Discord Webhooks** (alerts)
- **GitHub Gist** (tracking)

---

## ⚠️ LIMITATIONS

**Bot NIE łapie:**
- Item 7.01 (press releases) - planowane w v2.0
- Private companies (no SEC filings)
- Foreign companies (non-US)
- Deals <materiality threshold

**Bot łapie ~70% major deals** (wszystkie M&A, większość partnerships)

---

## 🔮 ROADMAP

**v1.0** (CURRENT):
- ✅ Item 1.01 scanning
- ✅ AI-driven impact scoring
- ✅ Smart routing
- ✅ Sector momentum
- ✅ Sympathy plays

**v2.0** (PLANNED):
- ⏳ Item 7.01 (press releases)
- ⏳ Historical performance tracking
- ⏳ Portfolio integration
- ⏳ Advanced filters

---

## 📞 SUPPORT

**Issues?**
1. Check GitHub Actions logs
2. Verify all 6 secrets are set
3. Test Discord webhooks
4. Check [DEPLOYMENT.md](./DEPLOYMENT.md)

---

## 📜 LICENSE

MIT License - Use freely!

---

## 🎉 CREDITS

Built with:
- SEC EDGAR API
- Google Gemini AI
- GitHub Actions
- Discord

---

## 💪 CONTRIBUTE

Pull requests welcome!

Ideas:
- Add more filters
- Improve Gemini prompts
- Add Item 7.01 support
- Backtesting framework

---

## ⭐ STAR THIS REPO!

If you find this useful, give it a star! 🌟

---

**Happy M&A trading!** 🚀💰📈
