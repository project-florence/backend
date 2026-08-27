# LLM Sağlayıcıları

Bu dosya iki şeyi bir arada tutar:

1. **Florence'ta tanımlı sağlayıcılar** — `src/llm/providers.py` kataloğunun insan okunur karşılığı.
   Kataloğa bir sağlayıcı eklendiğinde **bu dosya da güncellenir**.
2. **Referans liste** — [Hermes Agent sağlayıcı entegrasyonları](https://hermes-agent.nousresearch.com/docs/integrations/providers/)
   sayfasındaki sağlayıcılar, aday havuzu olarak.

**Tarih:** 2026-08-27 · **Katalog:** `src/llm/providers.py` · **Plan:** `../REFACTOR_PLAN.md`

Sağlayıcı seçimi ile base URL **ayrılamaz** — birlikte çözülür, birlikte doğrulanır.
Bu tasarımın sebebi 2026-08-26 arızasıdır: `.env`'de model `ox-alpha-free` yapıldı, URL
`zen/go/v1`'de kaldı, model o uç noktada yoktu ve digest üç slot boyunca sessizce düştü.

API anahtarları `llm_providers` tablosunda **AES-256-GCM ile şifreli** tutulur; env'de yalnız
`FLORENCE_MASTER_KEY` bulunur. Aşağıdaki referans tablodaki env değişkeni isimleri Hermes'in
kendi konvansiyonudur, Florence bunları kullanmaz.

---

## 1. Florence kataloğunda tanımlı olanlar

| id | base_url | api_style | reasoning_param | Doğrulama |
|---|---|---|---|---|
| `openai` | `https://api.openai.com/v1` | openai-chat | `openai_reasoning_effort` | ✅ dolaylı¹ |
| `anthropic` | `https://api.anthropic.com` | anthropic | `anthropic_thinking` | ✅ resmi doküman |
| `xai` | `https://api.x.ai/v1` | openai-chat | `openai_reasoning_effort` | ⚠️ models_url türetildi² |
| `groq` | `https://api.groq.com/openai/v1` | openai-chat | `openai_reasoning_effort` | ✅ resmi doküman |
| `deepseek` | `https://api.deepseek.com` | openai-chat | — | ✅ resmi doküman |
| `mistral` | `https://api.mistral.ai/v1` | openai-chat | — | ✅ resmi doküman |
| `openrouter` | `https://openrouter.ai/api/v1` | openai-chat | `openrouter_reasoning` | ✅ resmi doküman |
| `opencode-zen` | `https://opencode.ai/zen/v1` | openai-chat | — | ✅ canlı (63 model, 8 ücretsiz) ⁴ |
| `opencode-go` | `https://opencode.ai/zen/go/v1` | openai-chat | — | ✅ canlı (31 model, ücretsiz yok) ⁴ |
| `ollama-cloud` | `https://ollama.com/v1` | openai-chat | — | ✅ Hermes ile çapraz doğrulandı³ |
| `ollama-local` | `http://localhost:11434/v1` | openai-chat | — | ✅ resmi doküman |
| `openai-compatible` | *(seçimle birlikte saklanır)* | openai-chat | — | — |

¹ `platform.openai.com` bot engeli (403) verdiği için doğrudan çekilemedi; arama + SDK varsayılanıyla çapraz doğrulandı.
² `base_url` doğrudan doğrulandı, `models_url` OpenAI-uyumluluk konvansiyonundan türetildi — resmi sayfada teyit edilemedi.
⁴ **`/models` roster'ı anahtarsız cevap verir, ama `/chat/completions` anahtar ister** — anahtarsız istek `401 Invalid API key` döndü (2026-08-27 canlı deneme). Bu ikisini karıştırmak, doğrulamayı geçip her çağrıda 401 alan bir seçim yazılmasına yol açar. Gerçekten anahtarsız tek sağlayıcı `ollama-local`.
³ Katalog yazılırken resmi `docs.ollama.com` yalnız yerel `/v1`'i belgeliyordu; Hermes sayfası `ollama.com/v1` diyerek bağımsız teyit sağladı.

### `reasoning_param` neden bazılarında boş

`—` işareti "bu sağlayıcıya reasoning parametresi gönderilmez" demektir. Üç ayrı sebep var:

- **`deepseek`** — `deepseek-reasoner` sabit bir CoT modelidir, ayarlanabilir bir effort parametresi yoktur.
- **`opencode-zen` / `opencode-go`** — bunlar gateway/proxy. Arkadaki model değişkendir, sabit bir
  reasoning parametresi kör kör gönderilemez. 2026-08-26 arızası tam olarak bunun yapılmasıydı.
- **`mistral`, `ollama-*`, `openai-compatible`** — belgelenmiş bir reasoning parametresi yok.

Bundan **ayrı** ve daha genel bir kural: `output_type` bir pydantic modeli olan ajanlarda
(digest, rapor) reasoning **her zaman kapalıdır**, sağlayıcı ne olursa olsun. Sebep, reasoning
token'larının şema ayrıştırmasını bozup hata fırlatmasıdır. Kural
`src/llm/settings.py::structured_output_forbids_reasoning()` içinde ifade edilir.

---

## 2. Referans: Hermes Agent sağlayıcıları

Aday havuzu. **Durum** sütunu: ✅ Florence'ta var · ⬜ aday · ➖ kapsam dışı.

`nous` (Nous Portal) istek üzerine listelenmedi.

### Bulut API sağlayıcıları

| Sağlayıcı | id | Uç nokta | Kimlik | Durum |
|---|---|---|---|---|
| OpenAI API | `openai-api` | `api.openai.com/v1` | `OPENAI_API_KEY` | ✅ `openai` |
| Anthropic | `anthropic` | `api.anthropic.com` | `ANTHROPIC_API_KEY` / OAuth | ✅ |
| OpenRouter | `openrouter` | `openrouter.ai/api/v1` | `OPENROUTER_API_KEY` | ✅ |
| DeepSeek | `deepseek` | `api.deepseek.com/v1` | `DEEPSEEK_API_KEY` | ✅ ⁵ |
| xAI (Grok) | `xai` | `api.x.ai/v1` | `XAI_API_KEY` | ✅ |
| Ollama Cloud | `ollama-cloud` | `ollama.com/v1` | `OLLAMA_API_KEY` | ✅ |
| OpenCode Zen | `opencode-zen` | — | `OPENCODE_ZEN_API_KEY` | ✅ |
| OpenCode Go | `opencode-go` | — | `OPENCODE_GO_API_KEY` | ✅ |
| OpenCode Free | `opencode-free` | — | anahtarsız | ⬜ ⁷ |
| Fireworks AI | `fireworks` | `api.fireworks.ai/inference/v1` | `FIREWORKS_API_KEY` | ⬜ |
| NovitaAI | `novita` | `api.novita.ai/openai/v1` | `NOVITA_API_KEY` | ⬜ |
| GMI Cloud | `gmi` | `api.gmi-serving.com/v1` | `GMI_API_KEY` | ⬜ |
| Hugging Face | `huggingface` | `router.huggingface.co/v1` | `HF_TOKEN` | ⬜ |
| Google Gemini | `gemini` | — | `GOOGLE_API_KEY` | ⬜ |
| Google Vertex AI | `vertex` | `vertexai.googleapis.com` | `VERTEX_CREDENTIALS_PATH` | ⬜ |
| Azure AI Foundry | `azure-foundry` | `YOUR.openai.azure.com` | Azure kimlik | ⬜ |
| AWS Bedrock | `bedrock` | AWS SDK | AWS kimlik | ⬜ |
| NVIDIA NIM | `nvidia` | `build.nvidia.com` / `localhost:8000/v1` | `NVIDIA_API_KEY` | ⬜ |
| MiniMax | `minimax` | `api.minimax.io` | `MINIMAX_API_KEY` | ⬜ |
| MiniMax China | `minimax-cn` | — | `MINIMAX_CN_API_KEY` | ⬜ |
| Kimi / Moonshot | `kimi-coding` | `api.moonshot.ai` | `KIMI_API_KEY` | ⬜ |
| Kimi / Moonshot CN | `kimi-coding-cn` | `api.moonshot.cn` | `KIMI_CN_API_KEY` | ⬜ |
| z.ai / GLM | `zai` | otomatik tespit | `GLM_API_KEY` | ⬜ |
| Qwen Cloud | `alibaba` | `dashscope.aliyuncs.com/v1` | `DASHSCOPE_API_KEY` | ⬜ |
| Alibaba Coding Plan | `alibaba-coding-plan` | `coding-intl.dashscope.aliyuncs.com/v1` | `DASHSCOPE_API_KEY` | ⬜ |
| StepFun | `stepfun` | `api.stepfun.com/v1` | `STEPFUN_API_KEY` | ⬜ |
| Arcee AI | `arcee` | — | `ARCEEAI_API_KEY` | ⬜ |
| Xiaomi MiMo | `xiaomi` | — | `XIAOMI_API_KEY` | ⬜ |
| Tencent TokenHub | `tencent-tokenhub` | — | `TOKENHUB_API_KEY` | ⬜ |
| Kilo Code | `kilocode` | — | `KILOCODE_API_KEY` | ⬜ |
| CommandCode | `commandcode` | — | `COMMANDCODE_API_KEY` | ⬜ |
| CommandCode Anthropic | `commandcode-anthropic` | — | `COMMANDCODE_API_KEY` | ⬜ |
| AI Gateway | `ai-gateway` | — | `AI_GATEWAY_API_KEY` | ⬜ |
| Actual Computer | `actual` | `api.actual.inc/v1` / `localhost:8080` | `ACTUAL_API_KEY` | ⬜ |
| LM Studio | `lmstudio` | `localhost:1234/v1` | `LM_API_KEY` (ops.) | ⬜ |
| GitHub Copilot | `copilot` | `api.githubcopilot.com` | `COPILOT_GITHUB_TOKEN` | ➖ ⁷ |
| GitHub Copilot ACP | `copilot-acp` | yerel alt süreç | `HERMES_COPILOT_ACP_COMMAND` | ➖ ⁷ |
| OpenAI Codex | `openai-codex` | `api.openai.com` | device code OAuth | ➖ ⁷ |
| MiniMax OAuth | `minimax-oauth` | `api.minimax.io/anthropic` | tarayıcı OAuth | ➖ ⁷ |
| xAI Grok OAuth | `xai-oauth` | — | tarayıcı OAuth | ➖ ⁷ |
| Qwen OAuth | `qwen-oauth` | `portal.qwen.ai/v1` | tarayıcı OAuth | ➖ ⁷ |

⁵ Hermes `api.deepseek.com/v1` diyor, Florence kataloğu `https://api.deepseek.com` tutuyor (resmi dokümandaki OpenAI-format base'i). İşlevsel fark yok ama **bir kez canlı teyit edilmeli**.
⁶ Anahtarsız ücretsiz katman. Florence kataloğundaki `opencode-zen` ücretsiz modeller barındırıyor ama chat için anahtar istiyor (bkz. dipnot ⁴), dolayısıyla gerçekten anahtarsız bir giriş olarak ayrı değer taşıyabilir — değerlendirilmeli.
⁷ Tarayıcı/device OAuth gerektiriyor. Florence sunucu tarafında başsız (headless) çalışır; interaktif oturum akışı uygun değil.

### Kendi barındırdıkların

Hermes bunların hepsini tek bir `custom` kimliği altında topluyor. Florence'ta karşılığı
`openai-compatible` (URL seçimle birlikte saklanır) ve yerel Ollama için ayrıca `ollama-local`.

| Sağlayıcı | Uç nokta | Not | Durum |
|---|---|---|---|
| Ollama | `localhost:11434/v1` | anahtarsız | ✅ `ollama-local` |
| vLLM | `localhost:8000/v1` | araç çağrısı için `--enable-auto-tool-choice` gerekir | ✅ `openai-compatible` |
| SGLang | `localhost:30000/v1` | `--tool-call-parser` gerekir | ✅ `openai-compatible` |
| llama.cpp | `localhost:8080/v1` | araç çağrısı için `--jinja` gerekir | ✅ `openai-compatible` |
| LiteLLM Proxy | `localhost:4000/v1` | 100+ arka uç | ✅ `openai-compatible` |
| ClawRouter | `localhost:8402/v1` | USDC cüzdan | ✅ `openai-compatible` |

---

## 3. Sağlayıcı eklerken

1. `src/llm/providers.py` kataloğuna `ProviderSpec` ekle. **Base URL'i tahmin etme** — resmi
   dokümandan doğrula, doğrulayamıyorsan `verified=False` ve `notes` ile işaretle.
2. `models_url`'i mümkünse canlı çağırıp teyit et; `llm set` doğrulaması buna dayanıyor.
3. `reasoning_param`'ı ancak belgelenmişse doldur. Emin değilsen `None` bırak — kör göndermek
   2026-08-26 arızasının ta kendisiydi.
4. **Bu dosyanın 1. bölümüne satır ekle.**
5. Test: katalog değişmezleri `tests/test_llm_foundation.py` içinde kontrol ediliyor.
