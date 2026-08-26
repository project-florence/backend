"""LLM saglayici kataloguu -- kodda sabit, kullanici verisi DEGIL.

Bugun bu bilgi uc ayri dosyaya dagilmis durumdaydi (``src/clients/llm.py``,
``src/services/digest/agent.py``, ``src/services/report/__init__.py``) ve iki
farkli sekilde ifade ediliyordu. Bu modul onu tek bir yerde normalize eder.
Saglayici secimi ile base URL'in ayrilamamasi tasarimin cekirdegi: ``resolve()``
tek bir ``"saglayici/model"`` spec'ini birlikte cozer, boylece 2026-08-26
arizasindaki gibi model ve base URL'in birbirinden bagimsiz suruklenmesi
(``CUSTOM_MODEL`` degisip ``CUSTOM_URL`` sabit kalmasi) yapisal olarak
imkansiz hale gelir.

``api_key_env`` alani BILINCLI OLARAK YOK -- anahtarlar artik veritabaninda
sifreli (bkz. ``src/llm/crypto.py`` + ``src/llm/settings.py``), ortam
degiskeninde degil.

Dogrulama notu: her ProviderSpec bir ``verified`` bayragi ve ``notes`` alani
tasir. ``opencode-zen`` / ``opencode-go`` REFACTOR_PLAN.md'nin kendisinde
canli dogrulanmis olarak verildi. Digerleri bu adimda WebFetch/WebSearch ile
resmi dokumanlardan kontrol edildi (2026-08-27); tam dogrulanamayan alanlar
``verified=False`` ile isaretlendi ve ``notes``'ta neyin eksik oldugu yazili --
uydurma URL yok.
"""

from dataclasses import dataclass
from typing import Literal

ApiStyle = Literal["openai-chat", "anthropic", "responses"]


@dataclass(frozen=True)
class ProviderSpec:
    """Katalogdaki tek bir saglayicinin sabit tanimi."""

    id: str
    base_url: str | None  # yalniz "openai-compatible" icin None
    api_style: ApiStyle
    models_url: str | None  # canli roster ucnoktasi (varsa)
    reasoning_param: str | None  # bu API stilinde reasoning'in ifade edilis bicimi
    reasoning_values: frozenset[str]  # kabul edilen degerler (reasoning_param None ise bos)
    supports_tools: bool
    verified: bool  # resmi dokumandan WebFetch/WebSearch ile dogrulandi mi
    notes: str = ""  # dogrulama durumu / bilinen kisitlar


class InvalidModelSpec(ValueError):
    """``resolve()``'a gecersiz bicimde veya bilinmeyen bir saglayiciyla spec verildi."""


PROVIDERS: dict[str, ProviderSpec] = {
    "openai": ProviderSpec(
        id="openai",
        base_url="https://api.openai.com/v1",
        api_style="openai-chat",
        models_url="https://api.openai.com/v1/models",
        reasoning_param="openai_reasoning_effort",
        reasoning_values=frozenset({"minimal", "low", "medium", "high"}),
        supports_tools=True,
        verified=True,
        notes=(
            "platform.openai.com/docs dogrudan WebFetch ile 403 verdi (bot engeli); "
            "base_url ve /v1/models WebSearch ile ve openai-python SDK'nin varsayilan "
            "base_url'i ile capraz dogrulandi. reasoning_values gpt-5 serisi icin "
            "'minimal' dahil genisletildi, eski modellerde 'minimal' gecersiz olabilir."
        ),
    ),
    "anthropic": ProviderSpec(
        id="anthropic",
        base_url="https://api.anthropic.com",
        api_style="anthropic",
        models_url="https://api.anthropic.com/v1/models",
        reasoning_param="anthropic_thinking",
        reasoning_values=frozenset({"low", "medium", "high"}),
        supports_tools=True,
        verified=True,
        notes=(
            "platform.claude.com/docs/en/api/overview (docs.anthropic.com'dan "
            "301 yonlendirme) dogrudan WebFetch ile dogrulandi: base URL "
            "'https://api.anthropic.com', Models API 'GET /v1/models'. "
            "reasoning_values burada effort-tier soyutlamasi; gercek istekte "
            "budget_tokens'e cevrilmesi Adim 2'nin isi."
        ),
    ),
    "xai": ProviderSpec(
        id="xai",
        base_url="https://api.x.ai/v1",
        api_style="openai-chat",
        models_url="https://api.x.ai/v1/models",
        reasoning_param="openai_reasoning_effort",
        reasoning_values=frozenset({"low", "high"}),
        supports_tools=True,
        verified=False,
        notes=(
            "base_url 'https://api.x.ai/v1' docs.x.ai/docs/overview WebFetch ile "
            "dogrudan dogrulandi (OpenAI-uyumlu oldugu da acikca yaziyor). "
            "models_url bu sayfada acikca verilmedi -- OpenAI-uyumluluk "
            "konvansiyonundan (/v1/models) turetildi, TEYIT EDILMEDI. "
            "reasoning_values grok-3-mini docs'undaki bilinen low/high "
            "kisitina dayanir, bu oturumda dogrudan fetch edilmedi."
        ),
    ),
    "groq": ProviderSpec(
        id="groq",
        base_url="https://api.groq.com/openai/v1",
        api_style="openai-chat",
        models_url="https://api.groq.com/openai/v1/models",
        reasoning_param="openai_reasoning_effort",
        reasoning_values=frozenset({"low", "medium", "high"}),
        supports_tools=True,
        verified=True,
        notes=(
            "console.groq.com/docs/api-reference WebFetch ile dogrudan "
            "dogrulandi: base_url ve /models ucnoktasi acikca yazili. "
            "reasoning_effort yalniz belirli modellerde (ör. gpt-oss ailesi) "
            "gecerli -- tum Groq modelleri desteklemez, bu Adim 2'de model "
            "bazinda ele alinmali."
        ),
    ),
    "deepseek": ProviderSpec(
        id="deepseek",
        base_url="https://api.deepseek.com",
        api_style="openai-chat",
        models_url="https://api.deepseek.com/models",
        reasoning_param=None,
        reasoning_values=frozenset(),
        supports_tools=True,
        verified=True,
        notes=(
            "api-docs.deepseek.com WebFetch ile dogrudan dogrulandi: base_url "
            "'https://api.deepseek.com' (OpenAI formati icin), models "
            "ucnoktasi 'GET /models' olarak belgelenmis (tam base+path bu "
            "adimda birlestirildi). reasoning_param BILINCLI OLARAK None: "
            "deepseek-reasoner sabit bir CoT modelidir, ayarlanabilir bir "
            "reasoning-effort parametresi yok. NOT: 2026-08-26 arizasi bundan "
            "kaynaklanmadi -- eski kod deepseek'e effort='none' gonderiyordu, "
            "hata ADI deepseek ICERMEYEN bir modele (ox-alpha-free) "
            "effort='medium' gonderilmesiydi."
        ),
    ),
    "mistral": ProviderSpec(
        id="mistral",
        base_url="https://api.mistral.ai/v1",
        api_style="openai-chat",
        models_url="https://api.mistral.ai/v1/models",
        reasoning_param=None,
        reasoning_values=frozenset(),
        supports_tools=True,
        verified=True,
        notes=(
            "docs.mistral.ai/api WebFetch ile dogrudan dogrulandi: "
            "'https://api.mistral.ai/v1/chat/completions' ve "
            "'https://api.mistral.ai/v1/models' acikca yazili. "
            "reasoning_param None: standart chat completions'ta genel-gecer "
            "bir effort parametresi belgelenmedi (Magistral gibi reasoning "
            "modelleri farkli bir mekanizma kullanir, bu oturumda dogrulanmadi)."
        ),
    ),
    "openrouter": ProviderSpec(
        id="openrouter",
        base_url="https://openrouter.ai/api/v1",
        api_style="openai-chat",
        models_url="https://openrouter.ai/api/v1/models",
        reasoning_param="openrouter_reasoning",
        reasoning_values=frozenset({"low", "medium", "high", "xhigh"}),
        supports_tools=True,
        verified=True,
        notes=(
            "openrouter.ai/docs/api-reference/overview WebFetch ile base_url "
            "dogrulandi; models ucnoktasinin tam URL'si WebSearch ile "
            "dogrulandi (resmi referans sayfasi bu oturumda 404 verdi ama "
            "endpoint genel bilgi + arama sonuclariyla tutarli). Gercek "
            "istekte 'extra_body.reasoning.effort' olarak gonderilir -- "
            "'openrouter_reasoning' bu adimda sadece isim/normalizasyon, "
            "gercek extra_body insasi Adim 2'nin isi."
        ),
    ),
    "opencode-zen": ProviderSpec(
        id="opencode-zen",
        base_url="https://opencode.ai/zen/v1",
        api_style="openai-chat",
        models_url="https://opencode.ai/zen/v1/models",
        reasoning_param=None,
        reasoning_values=frozenset(),
        supports_tools=True,
        verified=True,
        notes=(
            "Talimatta canli dogrulanmis olarak verildi (63 model, 8 ucretsiz, "
            "auth gerektirmiyor). reasoning_param BILINCLI OLARAK None: bu bir "
            "gateway/proxy -- arkasindaki model degisebilir (2026-08-26 "
            "arizasinda oldugu gibi), sabit bir reasoning parametresi kor "
            "kor gonderilemez. Hangi reasoning'in ne zaman uygulanacagi "
            "Adim 2'de model bazinda ele alinmali."
        ),
    ),
    "opencode-go": ProviderSpec(
        id="opencode-go",
        base_url="https://opencode.ai/zen/go/v1",
        api_style="openai-chat",
        models_url="https://opencode.ai/zen/go/v1/models",
        reasoning_param=None,
        reasoning_values=frozenset(),
        supports_tools=True,
        verified=True,
        notes=(
            "Talimatta canli dogrulanmis olarak verildi (31 model, ucretsiz "
            "yok, auth gerektirmiyor). reasoning_param None: opencode-zen ile "
            "ayni gerekce -- gateway/proxy, arkasindaki model degisken."
        ),
    ),
    "ollama-cloud": ProviderSpec(
        id="ollama-cloud",
        base_url="https://ollama.com/v1",
        api_style="openai-chat",
        models_url="https://ollama.com/v1/models",
        reasoning_param=None,
        reasoning_values=frozenset(),
        supports_tools=True,
        verified=False,
        notes=(
            "docs.ollama.com/api/openai-compatibility resmi sayfasi WebFetch "
            "ile dogrudan okundu ama YALNIZCA yerel 'http://localhost:11434/v1' "
            "ornegi iceriyor, bulut (ollama.com) icin ayri bir base_url "
            "belgelemiyor. 'https://ollama.com/v1' degeri ikincil kaynaklardan "
            "(WebSearch: blog/topluluk yazilari) turetildi, RESMI DOKUMANDA "
            "TEYIT EDILEMEDI. Kullanmadan once canli 'llm models ollama-cloud' "
            "ile ayrica dogrulanmali (Adim 4)."
        ),
    ),
    "ollama-local": ProviderSpec(
        id="ollama-local",
        base_url="http://localhost:11434/v1",
        api_style="openai-chat",
        models_url="http://localhost:11434/api/tags",
        reasoning_param=None,
        reasoning_values=frozenset(),
        supports_tools=True,
        verified=True,
        notes=(
            "docs.ollama.com/api/openai-compatibility WebFetch ile dogrudan "
            "dogrulandi: 'base_url=\"http://localhost:11434/v1/\"'. "
            "models_url kasitli olarak OpenAI-uyumlu '/v1/models' degil, "
            "Ollama'nin yerel modelleri listeleyen native ucnoktasi "
            "'/api/tags' -- bu, Ollama'nin genel API referansinda iyi "
            "belgelenmis, /v1/models'in local modda var oldugu bu oturumda "
            "ayrica teyit edilmedi."
        ),
    ),
    "openai-compatible": ProviderSpec(
        id="openai-compatible",
        base_url=None,  # deger secimle birlikte llm_providers.base_url'den gelir
        api_style="openai-chat",
        models_url=None,
        reasoning_param=None,
        reasoning_values=frozenset(),
        supports_tools=True,
        verified=True,
        notes=(
            "Joker/genel amacli girdi -- sabit bir dis URL yok, dogrulanacak "
            "bir sey yok. base_url zorunlu olarak llm_providers tablosundan "
            "gelir (bkz. src/llm/settings.py::resolve_purpose)."
        ),
    ),
}


@dataclass(frozen=True)
class Resolved:
    """``resolve()`` sonucu: saglayici tanimi + spec'ten cikarilan model adi."""

    provider: ProviderSpec
    model: str


def resolve(spec: str) -> Resolved:
    """``"saglayici/model"`` bicimindeki bir spec'i saglayici + model olarak cozer.

    Model adinin kendisi de "/" icerebilir (ornegin OpenRouter model id'leri:
    ``"openrouter/anthropic/claude-3.5-sonnet"``), bu yuzden yalnizca ILK "/"
    ayrac olarak kullanilir.

    Saglayici ve model ASLA birbirinden bagimsiz ayarlanamaz -- bu fonksiyon
    tasarimin cekirdegi: bir taraf digerinden yalitilmis sekilde
    degistirilemez, ikisi birlikte gelir.
    """
    if not spec or "/" not in spec:
        raise InvalidModelSpec(
            f"gecersiz model spec'i: {spec!r} -- beklenen bicim 'saglayici/model'"
        )
    provider_id, model = spec.split("/", 1)
    if not provider_id or not model:
        raise InvalidModelSpec(
            f"gecersiz model spec'i: {spec!r} -- saglayici ve model bos olamaz"
        )
    provider_spec = PROVIDERS.get(provider_id)
    if provider_spec is None:
        raise InvalidModelSpec(
            f"bilinmeyen saglayici: {provider_id!r} -- katalogda yok "
            f"(mevcut: {sorted(PROVIDERS)})"
        )
    return Resolved(provider=provider_spec, model=model)
