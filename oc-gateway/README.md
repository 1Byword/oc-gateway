# بوابة ذكاء اصطناعي سحابية لـ OpenCode (OC-Gateway)

بوابة متوافقة مع OpenAI تعمل على خادم سحابي، تنقل طلباتك تلقائيًا بين مزودين مجانيين (OpenRouter / Groq / NVIDIA NIM / أي مزود آخر)
عند حدوث 429 أو نفاد الحصة أو انقطاع الخدمة، مع حفظ سياق العمل. **لا يوجد أي استدلال محلي**؛ جهازك يشغّل OpenCode فقط.

English summary: an OpenAI-compatible gateway (Flask + SQLite) that does health-aware failover across free-tier providers,
verifies capabilities by probing, never splices a half-finished stream, and ships session/project-state tooling. See `docs/`.

## البدء السريع
1. على جهازك (Kali/WSL2): `python3 tools/genkeys.py` ← يطبع قيم `.env` للخادم + مفتاح العميل + رمز المدير (تُعرض مرة واحدة).
2. على الخادم: انسخ `.env.example` إلى `.env` وضع القيم، ثم عدّل `config/providers.yaml` (أسماء النماذج أمثلة يجب التحقق منها).
3. `docker compose up -d --build` ثم افتح لوحة التحكم عبر نفق SSH: `ssh -L 8080:127.0.0.1:8080 user@server` → http://127.0.0.1:8080/dashboard
4. في اللوحة: أضف مفاتيح المزودين → **Test connection** → **Probe capabilities** (إلزامي قبل استخدام الأدوات).
5. في مشروعك: `python3 tools/ocstate.py init` وانسخ `opencode.json` (غيّر `YOUR-DOMAIN`) ثم `export GATEWAY_KEY=...` وشغّل `opencode`.

## الأوامر اليومية
| الأمر | الوظيفة |
|---|---|
| `ocstate.py checkpoint "قبل التعديل"` | لقطة git للمشروع دون المساس بفرعك |
| `ocstate.py diff` / `restore REF --yes` | مقارنة / استرجاع |
| `ocstate.py resume` | يطبع تعليمات استئناف المهمة بعد انقطاع |
| `ocstate.py lint` | يفحص PROJECT_STATE.md بحثًا عن أسرار |
| `python3 tests/report.py` | يشغّل كل الاختبارات ويكتب TEST_REPORT.md |

الوثائق: `docs/ARCHITECTURE.md` · `docs/DEPLOYMENT.md` · `docs/SECURITY.md` · `docs/TROUBLESHOOTING.md` · `TEST_REPORT.md`
