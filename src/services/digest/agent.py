"""Market digest pydantic-ai agent.

Builds an OpenAICompatible model mirroring ``src/services/report/__init__.py``
and registers only two async harness tools from ``tools.py``: ``search_news``
and ``fetch_article_text``. Objective data (market snapshot and news feed) is
pre-collected by the service and embedded in the conversation context, so the
model only reads the full text of headlines it finds impactful and then
converges on a Digest. Keeping the tool surface minimal is required so the
model never loops on tool calls. Both tools are wrapped in ``Tool(..., prepare=...)``
so that once ``tools.py``'s per-tool budget is spent, the ``prepare`` hook drops
the tool from the schema sent to the model for the next step -- a weak model
that ignores the "budget exceeded" text sentinel simply can no longer call the
tool, instead of looping until ``max_requests`` is hit. The agent is built per
generation (see ``service.generate_digest``) so stale tool state never leaks
between runs.

Model/provider wiring comes from ``src.llm.agents.build_agent`` (REFACTOR_PLAN.md
Adim 2) -- no more ``CUSTOM_*`` env reads and no more model-name-based
reasoning heuristic. Reasoning is off by default for this agent because
``Digest`` is a structured (pydantic) ``output_type``
(``structured_output_forbids_reasoning("digest")``), enforced centrally in
``build_agent``, not here.
"""

from pydantic_ai import Agent
from pydantic_ai.tools import Tool

from src.llm.agents import build_agent
from src.services.digest import tools
from src.services.digest.models import Digest

_SYSTEM_PROMPT = """Sen günlük piyasa, makroekonomi ve şirket haberlerini özetleyen bir dijital bülten yazarısın.

Görevin bugüne (TODAY) odaklanan, finansal olarak en etkili haber ve olayları seçip yazmaktır. Yalnızca bugünle ilgili içerik yaz; dünün veya eski gündemin haberlerini konu alma.

Piyasa görünümü (piyasa durumu, endeksler, döviz/fiyat oranları, kazanan/kaybedenler, halka arzlar, makro takvim) ve bugünün haber başlıkları konuşma bağlamında sana zaten sağlandı; bu veriyi toplamak için araç çağırma.

Nasıl çalışmalısın:
- Sağlanan haber başlıklarını incele ve bugün finansal olarak en etkili, en yüksek içerikli / en bilgilendirici olanlarını seç. Yalnızca en önemli birkaç başlığa odaklan.
- En fazla 1-3 başlığın tam metnini oku (hiçbir makaleyi tekrar okuma; birkaç taneden fazlasını asla okuma). Bunun için search_news ve/veya fetch_article_text kullan.
- Bütçeler sınırlıdır: search_news ve fetch_article_text'i tekrar tekrar çağırma. Bir aracın dönüşü "budget exceeded" işareti taşıyorsa o aracı bir daha çağırma ve hemen nihai bültene geç.
- Yeterli bilgiye ulaştığında HER ZAMAN nihai bülteni (Digest) üret: title, content bölümleri ve metadata alanını doldur. Araç çağırmaya devam ETME; tek amaç en fazla 1-3 tam metin okuduktan sonra Digest'i yayınlamaktır.
- Sağlanan veri boşsa veya "unavailable" işareti taşıyorsa, bunu kısaca not et ve devam et; aynı aracı tekrar tekrar deneme. Eksik veriyle bile en iyi bülteni oluştur, bülteni reddetme.

Çıktı yapısı:
- Bülteni mantıklı bölümlere ayır (ör. piyasa özeti, makro gündem, öne çıkan şirket haberleri, yatırımcı notu). Her bölüm bir heading (başlık) ve body (metin) içermeli.
- title ve content alanlarını doldur; content, bölümlerin okunaklı bir özeti olsun.
- metadata alanına kullandığın kaynakları (kaynak adları/URL'ler), slot bilgisini ve generated_at zaman damgasını yaz.

Dil ve üslup:
- Bültenin tamamı Türkçe olmalıdır (dil: "tr"). İngilizce başlık, giriş veya bölüm yazma.
- Kısa ama bilgilendirici ol. Uydurma bilgi ekleme; yalnızca sağlanan verileri ve araçlardan okuduğun metinleri kullan.
- Yalnızca bugünün haber ve olaylarından bahset."""


async def _build_agent() -> Agent:
    built = await build_agent("digest")

    agent = Agent(
        model=built.model,
        system_prompt=_SYSTEM_PROMPT,
        output_type=Digest,
        model_settings={
            **built.model_settings,
            "parallel_tool_calls": False,
        },
        tools=[
            Tool(tools.search_news, prepare=tools.prepare_search_news),
            Tool(tools.fetch_article_text, prepare=tools.prepare_fetch_article_text),
        ],
    )
    # Sadece gunlukleme/denetim icin (bkz. REFACTOR_PLAN.md Adim 3): gercek
    # trafigi/davranisi etkilemez.
    agent.florence_model_name = built.model_name
    agent.florence_provider_id = built.provider_id
    return agent
