"""Market digest pydantic-ai agent.

Builds an OpenAICompatible model mirroring ``src/services/report/__init__.py``
and registers only two async harness tools from ``tools.py``: ``search_news``
and ``fetch_article_text``. Objective data (market snapshot and news feed) is
pre-collected by the service and embedded in the conversation context, so the
model only reads the full text of headlines it finds impactful and then
converges on a Digest. Keeping the tool surface minimal is required so the
model never loops on tool calls. The agent is built per generation (see
``service.generate_digest``) so stale tool state never leaks between runs.
"""

import os

from pydantic_ai import Agent
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.openai import OpenAIProvider

from src.core.config import get_config
from src.services.digest import tools
from src.services.digest.models import Digest

_SYSTEM_PROMPT = """Sen günlük piyasa, makroekonomi ve şirket haberlerini özetleyen bir dijital bülten yazarısın.

Görevin bugüne (TODAY) odaklanan, finansal olarak en etkili haber ve olayları seçip yazmaktır. Yalnızca bugünle ilgili içerik yaz; dünün veya eski gündemin haberlerini konu alma.

Piyasa görünümü (piyasa durumu, endeksler, döviz/fiyat oranları, kazanan/kaybedenler, halka arzlar, makro takvim) ve bugünün haber başlıkları konuşma bağlamında sana zaten sağlandı; bu veriyi toplamak için araç çağırma.

Nasıl çalışmalısın:
- Sağlanan haber başlıklarını incele ve bugün finansal olarak en etkili olanları seç.
- Yalnızca seçtiğin bir başlığın tam metnini okumak istersen search_news ile arama yap ve/veya fetch_article_text ile tam metnini oku. Seçici ve sınırlı ol; aynı aracı gereksiz yere tekrar tekrar çağırma.
- Yeterli bilgiye ulaştığında HER ZAMAN nihai bülteni (Digest) üret: title, content bölümleri ve metadata alanını doldur. Araç çağırmaya devam ETME.
- Sağlanan veri boşsa veya "unavailable" işareti taşıyorsa, bunu kısaca not et ve devam et; aynı aracı tekrar tekrar deneme. Eksik veriyle bile en iyi bülteni oluştur, bülteni reddetme.

Çıktı yapısı:
- Bülteni mantıklı bölümlere ayır (ör. piyasa özeti, makro gündem, öne çıkan şirket haberleri, yatırımcı notu). Her bölüm bir heading (başlık) ve body (metin) içermeli.
- title ve content alanlarını doldur; content, bölümlerin okunaklı bir özeti olsun.
- metadata alanına kullandığın kaynakları (kaynak adları/URL'ler), slot bilgisini ve generated_at zaman damgasını yaz.

Dil ve üslup:
- Bültenin tamamı Türkçe olmalıdır (dil: "tr"). İngilizce başlık, giriş veya bölüm yazma.
- Kısa ama bilgilendirici ol. Uydurma bilgi ekleme; yalnızca sağlanan verileri ve araçlardan okuduğun metinleri kullan.
- Yalnızca bugünün haber ve olaylarından bahset."""


def _build_agent() -> Agent:
    digest_cfg = get_config()["digest"]
    model_name = os.getenv("CUSTOM_MODEL") or digest_cfg.get("model", "deepseek-v4-flash")
    base_url = os.getenv("CUSTOM_URL", "https://opencode.ai/zen/go/v1").rstrip("/")
    api_key = os.getenv("CUSTOM_API_KEY") or "not-needed"

    provider = OpenAIProvider(base_url=base_url, api_key=api_key)
    model = OpenAIChatModel(model_name, provider=provider)

    return Agent(
        model=model,
        system_prompt=_SYSTEM_PROMPT,
        output_type=Digest,
        model_settings={
            "openai_reasoning_effort": "none",
            "parallel_tool_calls": False,
        },
        tools=[
            tools.search_news,
            tools.fetch_article_text,
        ],
    )
