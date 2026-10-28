"""Помощник для учебного группового чата по испанскому языку (@polyglotatelbot).

Функции:
- Автоматическая расшифровка голосовых сообщений через локальный/серверный Whisper
  и публикация расшифровки ответом на голосовое сообщение на языке оригинала.
- Перевод текста между русским (ru), английским (en) и испанским (es) через локальный
  серверный Argos Translate API (без LLM и облачных фолбэков):
  * по явному упоминанию @polyglotatelbot в тексте;
  * по команде /translate (с текстом или ответом на сообщение);
  * по обращению «@polyglotatelbot переведи» ответом на текст или голосовое
    (для голосового используется сохранённая расшифровка, если она уже есть).
- Локальное определение языка (ru / en / es) без LLM с отсечением неподдерживаемых
  и неоднозначных текстов.
- Полная изоляция от публикаций в канал (main.py): работает строго в белом списке
  учебных чатов (ALLOWED_CHAT_IDS) и никогда не отправляет сообщения в канал.
"""

from collections import OrderedDict, deque
from concurrent.futures import Executor, ThreadPoolExecutor
from dataclasses import dataclass
import logging
import os
import re
import sys
import tempfile
import threading
import time
from typing import Callable, Dict, Iterable, List, Optional, Sequence, Set, Tuple

import requests

logger = logging.getLogger("polyglot.chat_assistant")

# --- КОНСТАНТЫ И ЯЗЫКИ ---
SUPPORTED_LANGS = ("ru", "en", "es")

LANG_LABELS = {
    "ru": "🇷🇺 Русский",
    "en": "🇬🇧 Английский",
    "es": "🇪🇸 Испанский",
}

TARGET_LANGS_FOR_SOURCE: Dict[str, Tuple[str, str]] = {
    "ru": ("en", "es"),
    "en": ("ru", "es"),
    "es": ("ru", "en"),
}

LANG_ALIASES: Dict[str, str] = {
    "ru": "ru",
    "rus": "ru",
    "russian": "ru",
    "рус": "ru",
    "русский": "ru",
    "русского": "ru",
    "по-русски": "ru",
    "en": "en",
    "eng": "en",
    "english": "en",
    "англ": "en",
    "английский": "en",
    "английского": "en",
    "по-английски": "en",
    "es": "es",
    "esp": "es",
    "spa": "es",
    "spanish": "es",
    "español": "es",
    "espanol": "es",
    "исп": "es",
    "испанский": "es",
    "испанского": "es",
    "по-испански": "es",
}

TELEGRAM_MESSAGE_LIMIT = 4096
DEFAULT_MAX_TEXT_LENGTH = 2000  # лимит сервиса перевода на stelve (TRANSLATE_MAX_CHARS)
DEFAULT_MAX_VOICE_DURATION_SEC = 300
DEFAULT_MAX_VOICE_SIZE_BYTES = 10 * 1024 * 1024  # 10 МБ
DEFAULT_MAX_CONCURRENT_TASKS = 4
DEFAULT_RATE_LIMIT_REQUESTS = 10
DEFAULT_RATE_LIMIT_WINDOW_SEC = 60.0
DEFAULT_DEDUP_CACHE_SIZE = 1000
DEFAULT_TRANSCRIPTION_CACHE_SIZE = 500


# --- ИСКЛЮЧЕНИЯ ---
class AssistantError(Exception):
    """Базовая ошибка помощника чата."""


class ServiceUnavailableError(AssistantError):
    """Ошибка недоступности или тайм-аута внешнего сервиса (Whisper / Argos / Telegram)."""


class AudioLimitExceededError(AssistantError):
    """Превышен лимит длительности или размера голосового сообщения."""


# --- КОНФИГУРАЦИЯ ---
def parse_chat_ids(raw: str) -> Set[int]:
    """Разбирает строку с ID чатов через запятую или пробел в множество int."""
    result: Set[int] = set()
    if not raw:
        return result
    for part in re.split(r"[,;\s]+", raw.strip()):
        if not part:
            continue
        try:
            result.add(int(part))
        except ValueError:
            logger.warning("Ignored invalid chat_id in configuration")
    return result


@dataclass
class AssistantConfig:
    bot_token: str
    bot_username: str = "polyglotatelbot"
    allowed_chat_ids: Set[int] = None  # type: ignore[assignment]
    channel_chat_id: Optional[str] = None
    argos_api_url: str = "http://127.0.0.1:8770"
    argos_api_key: Optional[str] = None
    argos_timeout: float = 30.0
    whisper_api_url: str = "http://127.0.0.1:8765/v1/audio/transcriptions"
    whisper_api_key: Optional[str] = None
    whisper_language: Optional[str] = None
    whisper_file_field: Optional[str] = None
    whisper_model: Optional[str] = None
    # whisper-turbo на CPU stelve: ~0,5-0,7 с на секунду аудио, плюс очередь сервиса
    whisper_timeout: float = 300.0
    heartbeat_file: Optional[str] = None
    telegram_timeout: float = 30.0
    max_voice_duration_sec: int = DEFAULT_MAX_VOICE_DURATION_SEC
    max_voice_size_bytes: int = DEFAULT_MAX_VOICE_SIZE_BYTES
    max_text_length: int = DEFAULT_MAX_TEXT_LENGTH
    max_concurrent_tasks: int = DEFAULT_MAX_CONCURRENT_TASKS
    rate_limit_requests: int = DEFAULT_RATE_LIMIT_REQUESTS
    rate_limit_window_sec: float = DEFAULT_RATE_LIMIT_WINDOW_SEC
    dedup_cache_size: int = DEFAULT_DEDUP_CACHE_SIZE
    transcription_cache_size: int = DEFAULT_TRANSCRIPTION_CACHE_SIZE
    poll_timeout_sec: int = 25

    def __post_init__(self):
        self.bot_username = (self.bot_username or "polyglotatelbot").lstrip("@").strip()
        if self.allowed_chat_ids is None:
            self.allowed_chat_ids = set()
        else:
            self.allowed_chat_ids = {int(cid) for cid in self.allowed_chat_ids}
        # Гарантируем, что канал для публикаций никогда не попадёт в список учебных чатов
        if self.channel_chat_id:
            try:
                self.allowed_chat_ids.discard(int(str(self.channel_chat_id).strip()))
            except ValueError:
                pass

    @classmethod
    def from_env(cls) -> "AssistantConfig":
        raw_allowed = (
            os.environ.get("ALLOWED_CHAT_IDS")
            or os.environ.get("ALLOWED_CHAT_ID")
            or os.environ.get("STUDY_CHAT_ID")
            or os.environ.get("TG_STUDY_CHAT_ID")
            or ""
        )
        return cls(
            bot_token=os.environ.get("TG_BOT_TOKEN", "").strip(),
            bot_username=os.environ.get("TG_BOT_USERNAME", "polyglotatelbot").strip(),
            allowed_chat_ids=parse_chat_ids(raw_allowed),
            channel_chat_id=os.environ.get("TG_CHAT_ID", "").strip() or None,
            argos_api_url=os.environ.get("ARGOS_API_URL", "http://127.0.0.1:8770").strip(),
            argos_api_key=os.environ.get("ARGOS_API_KEY", "").strip() or None,
            argos_timeout=float(os.environ.get("ARGOS_TIMEOUT", "30")),
            whisper_api_url=os.environ.get(
                "WHISPER_API_URL", "http://127.0.0.1:8765/v1/audio/transcriptions"
            ).strip(),
            whisper_api_key=os.environ.get("WHISPER_API_KEY", "").strip() or None,
            whisper_language=os.environ.get("WHISPER_LANGUAGE", "").strip() or None,
            whisper_file_field=os.environ.get("WHISPER_FILE_FIELD", "").strip() or None,
            whisper_model=os.environ.get("WHISPER_MODEL", "").strip() or None,
            whisper_timeout=float(os.environ.get("WHISPER_TIMEOUT", "300")),
            heartbeat_file=os.environ.get("HEARTBEAT_FILE", "").strip() or None,
            telegram_timeout=float(os.environ.get("TG_API_TIMEOUT", "30")),
            max_voice_duration_sec=int(
                os.environ.get("MAX_VOICE_DURATION_SEC", str(DEFAULT_MAX_VOICE_DURATION_SEC))
            ),
            max_voice_size_bytes=int(
                os.environ.get("MAX_VOICE_SIZE_BYTES", str(DEFAULT_MAX_VOICE_SIZE_BYTES))
            ),
            max_text_length=int(
                os.environ.get("MAX_TEXT_LENGTH", str(DEFAULT_MAX_TEXT_LENGTH))
            ),
            max_concurrent_tasks=int(
                os.environ.get("MAX_CONCURRENT_TASKS", str(DEFAULT_MAX_CONCURRENT_TASKS))
            ),
            rate_limit_requests=int(
                os.environ.get("RATE_LIMIT_REQUESTS", str(DEFAULT_RATE_LIMIT_REQUESTS))
            ),
            rate_limit_window_sec=float(
                os.environ.get("RATE_LIMIT_WINDOW_SEC", str(DEFAULT_RATE_LIMIT_WINDOW_SEC))
            ),
            poll_timeout_sec=int(os.environ.get("POLL_TIMEOUT_SEC", "25")),
        )


def redact_secrets(text: str, secrets: Sequence[Optional[str]]) -> str:
    """Удаляет секреты (например, токен бота в URL) из строк ошибок перед логированием."""
    sanitized = text or ""
    for secret in secrets:
        if secret:
            sanitized = sanitized.replace(secret, "<REDACTED>")
    sanitized = re.sub(r"bot\d+:[A-Za-z0-9_-]+", "bot<REDACTED>", sanitized)
    return sanitized


# --- ЛОКАЛЬНОЕ ОПРЕДЕЛЕНИЕ ЯЗЫКА (БЕЗ LLM) ---
@dataclass(frozen=True)
class LanguageDetectionResult:
    status: str  # "ok" | "ambiguous" | "unsupported" | "empty"
    lang: Optional[str] = None
    confidence: float = 0.0


# Символы кириллицы, не входящие в русский алфавит (украинский, белорусский, сербский и др.)
_NON_RUSSIAN_CYRILLIC = set("іїєґўђјљњћџѕІЇЄҐЎЂЈЉЊЋЏЅ")

# Диакритика латиницы, характерная для неподдерживаемых языков (pt, fr, de, it, tr, uz и др.),
# но не используемая в стандартном испанском или английском
_NON_ES_EN_LATIN_CHARS = set("ãõçâêîôûàèìòùäößœæğşıÃÕÇÂÊÎÔÛÀÈÌÒÙÄÖẞŒÆĞŞİ")

# Уникальные для испанского символы (среди пары EN/ES)
_SPANISH_SPECIFIC_CHARS = set("ñáéíóúüÑÁÉÍÓÚÜ¿¡")

# Списки частотных и однозначных слов для локального распознавания EN и ES
_SPANISH_WORDS = {
    "hola", "gracias", "buenos", "buenas", "dias", "días", "tardes", "noches",
    "por", "favor", "como", "cómo", "estas", "estás", "estoy", "esta", "está",
    "estamos", "estan", "están", "bien", "muy", "mucho", "mucha", "muchos",
    "muchas", "poco", "poca", "pocos", "todo", "toda", "todos", "todas",
    "nada", "algo", "alguien", "nadie", "siempre", "nunca", "jamás", "jamas",
    "también", "tambien", "tampoco", "pero", "porque", "cuando", "cuándo",
    "donde", "dónde", "quien", "quién", "cual", "cuál", "este", "esta",
    "estos", "estas", "ese", "esa", "esos", "esas", "aquel", "aquella",
    "nosotros", "nosotras", "vosotros", "vosotras", "ellos", "ellas",
    "usted", "ustedes", "yo", "tú", "el", "él", "ella", "la", "los", "las",
    "un", "una", "unos", "unas", "del", "al", "mi", "mis", "tu", "tus",
    "su", "sus", "nuestro", "nuestra", "nuestros", "nuestras", "tengo",
    "tienes", "tiene", "tenemos", "tienen", "hacer", "hago", "haces", "hace",
    "hacemos", "hacen", "quiero", "quieres", "quiere", "queremos", "quieren",
    "puedo", "puedes", "puede", "podemos", "pueden", "necesito", "necesitas",
    "necesita", "hablar", "hablo", "hablas", "habla", "hablamos", "hablan",
    "comer", "vivir", "trabajar", "estudiar", "aprender", "entender",
    "comprender", "escribir", "leer", "escuchar", "mirar", "ver", "saber",
    "conocer", "pensar", "creer", "decir", "digo", "dice", "vamos", "voy",
    "vas", "va", "van", "ser", "soy", "eres", "somos", "son", "era", "fue",
    "estar", "hay", "había", "ayer", "hoy", "mañana", "manana", "ahora",
    "después", "despues", "antes", "aquí", "aqui", "allí", "alli", "ahí",
    "casa", "trabajo", "tiempo", "vida", "mundo", "amigo", "amiga", "amigos",
    "hermano", "hermana", "madre", "padre", "hijo", "hija", "familia",
    "coche", "perro", "gato", "agua", "comida", "dinero", "ciudad", "libro",
    "escuela", "universidad", "pregunta", "respuesta", "palabra", "palabras",
    "frase", "verdad", "mentira", "bonito", "bonita", "hermoso", "hermosa",
    "grande", "grandes", "pequeño", "pequeno", "pequeña", "pequena", "nuevo",
    "nueva", "viejo", "vieja", "bueno", "buena", "malo", "mala", "mejor",
    "peor", "español", "espanol", "inglés", "ingles", "ruso", "rusa",
    "con", "sin", "para", "sobre", "entre", "hasta", "desde", "hacia",
    "según", "segun", "durante", "contra", "más", "mas", "menos", "tan",
    "tanto", "tanta", "sí", "claro", "vale", "adios", "adiós", "hasta",
    "luego", "pronto", "perdón", "perdon", "disculpa", "disculpe", "lo",
    "siento", "encantado", "encantada", "gusto", "conocerle", "conocerte",
    "qué", "que", "quiénes", "cuánto", "cuanto", "cuántos", "cuantos",
}

_ENGLISH_WORDS = {
    "hello", "hi", "hey", "thanks", "thank", "you", "please", "sorry",
    "excuse", "welcome", "goodbye", "bye", "morning", "evening", "afternoon",
    "night", "today", "tomorrow", "yesterday", "now", "later", "soon",
    "always", "never", "sometimes", "often", "usually", "already", "still",
    "yet", "just", "only", "also", "too", "very", "really", "quite",
    "almost", "enough", "much", "many", "more", "most", "less", "least",
    "few", "little", "some", "any", "every", "each", "all", "both", "either",
    "neither", "other", "another", "such", "same", "different", "own",
    "the", "and", "but", "or", "nor", "for", "yet", "so", "because",
    "although", "though", "while", "since", "unless", "until", "before",
    "after", "when", "whenever", "where", "wherever", "what", "whatever",
    "who", "whom", "whose", "which", "whichever", "why", "how", "whether",
    "this", "that", "these", "those", "here", "there", "where", "everywhere",
    "somewhere", "anywhere", "nowhere", "nothing", "something", "anything",
    "everything", "someone", "anyone", "everyone", "nobody", "anybody",
    "everybody", "i", "we", "they", "he", "she", "it", "my", "your", "his",
    "her", "its", "our", "their", "mine", "yours", "hers", "ours", "theirs",
    "myself", "yourself", "himself", "herself", "itself", "ourselves",
    "themselves", "am", "is", "are", "was", "were", "be", "been", "being",
    "have", "has", "had", "having", "do", "does", "did", "doing", "done",
    "will", "would", "shall", "should", "can", "could", "may", "might",
    "must", "ought", "need", "want", "wants", "wanted", "know", "knows",
    "knew", "known", "think", "thinks", "thought", "take", "takes", "took",
    "taken", "see", "sees", "saw", "seen", "come", "comes", "came", "look",
    "looks", "looked", "use", "uses", "used", "find", "finds", "found",
    "give", "gives", "gave", "given", "tell", "tells", "told", "work",
    "works", "worked", "working", "call", "try", "ask", "feel", "become",
    "leave", "put", "mean", "keep", "let", "begin", "seem", "help", "talk",
    "turn", "start", "show", "hear", "play", "run", "move", "like", "live",
    "believe", "hold", "bring", "happen", "write", "read", "speak", "speaking",
    "learn", "learning", "study", "studying", "understand", "translate",
    "translation", "language", "languages", "english", "spanish", "russian",
    "good", "great", "better", "best", "bad", "worse", "worst", "new", "old",
    "young", "big", "small", "large", "long", "short", "high", "low", "right",
    "wrong", "true", "false", "easy", "hard", "difficult", "important",
    "interesting", "beautiful", "nice", "happy", "sad", "ready", "sure",
    "clear", "free", "full", "empty", "open", "closed", "house", "home",
    "time", "day", "days", "week", "month", "year", "years", "world", "life",
    "hand", "part", "child", "children", "man", "men", "woman", "women",
    "person", "people", "place", "work", "book", "books", "school", "friend",
    "friends", "family", "water", "food", "money", "city", "car", "dog",
    "cat", "question", "answer", "word", "words", "sentence", "with",
    "without", "from", "into", "during", "including", "until", "against",
    "among", "throughout", "despite", "towards", "upon", "about", "above",
    "below", "between", "under", "over", "again", "further", "then", "once",
    "yes", "yeah", "nope",
}

# Слова-когнаты, которые сами по себе (без контекста) одинаково пишутся в EN и ES
_AMBIGUOUS_COGNATES = {
    "no", "ok", "okay", "hotel", "hospital", "doctor", "actor", "animal",
    "normal", "idea", "plan", "club", "bar", "taxi", "radio", "video",
    "chocolate", "banana", "drama", "crisis", "base", "control", "total",
    "final", "original", "universal", "social", "moral", "legal", "formal",
    "fatal", "vital", "central", "natural", "cultural", "personal", "general",
    "popular", "regular", "familiar", "similar", "particular", "singular",
    "plural", "simple", "triple", "probable", "terrible", "horrible",
    "visible", "invisible", "flexible", "noble", "cable", "motor", "director",
    "interior", "exterior", "superior", "inferior", "honor", "humor",
    "terror", "horror", "error", "color", "favor", "momento", "solo", "me",
}

# Маркеры других латинских языков (французский, немецкий, португальский, итальянский, узбекский)
_FOREIGN_LATIN_WORDS = {
    # French
    "bonjour", "merci", "beaucoup", "comment", "allez", "vous", "nous",
    "suis", "sont", "dans", "avec", "pour", "mais", "donc", "cette", "sont",
    "très", "avoir", "être", "faire",
    # German
    "guten", "morgen", "danke", "bitte", "nicht", "sind", "haben", "werden",
    "kann", "muss", "oder", "aber", "wenn", "weil", "ihnen", "sich",
    "auch", "noch", "schon", "immer", "alles", "leben", "sprechen", "deutsch",
    # Portuguese
    "obrigado", "obrigada", "você", "voce", "vocês", "tudo", "muito", "bom",
    "isso", "isto", "aquilo", "falar", "português", "portugues",
    # Italian
    "ciao", "grazie", "buongiorno", "buonasera", "arrivederci", "sono",
    "siamo", "della", "nella", "degli", "delle", "questo", "questa", "anche",
    "molto", "tutto", "tutti", "parlare", "italiano",
    # Uzbek (Latin)
    "salom", "rahmat", "yaxshi", "uchun", "bilan", "qanday", "mening",
    "sening", "uning", "bizning", "ular", "kerak", "bo‘lib", "o‘zbek",
}

# Слова, которые после испанского глагола "es" образуют типичное сказуемое
# (чтобы не путать испанское предложение "Es una buena idea" с кодом языка "es")
_SPANISH_POST_ES_WORDS = {
    "un", "una", "el", "la", "mi", "tu", "su", "nuestro", "nuestra",
    "muy", "tan", "más", "mas", "menos", "mejor", "peor", "verdad", "cierto",
    "posible", "imposible", "necesario", "importante", "bueno", "buena",
    "malo", "mala", "grande", "pequeño", "pequeno", "nuevo", "nueva",
    "como", "que", "porque", "para", "de", "en", "todo", "toda", "algo",
    "nada", "hora", "día", "dia", "tiempo", "fácil", "facil", "difícil", "dificil",
}

# Слова, которые после испанского предлога "en" образуют обстоятельство места/времени
_SPANISH_POST_EN_WORDS = {
    "el", "la", "los", "las", "un", "una", "unos", "unas", "mi", "mis",
    "tu", "tus", "su", "sus", "nuestro", "nuestra", "este", "esta", "estos",
    "estas", "ese", "esa", "aquel", "casa", "españa", "espana", "madrid",
    "méxico", "mexico", "español", "espanol", "verano", "invierno", "general",
    "realidad", "total", "cambio", "cuanto", "vez", "lugar", "medio",
}


def detect_language(text: str) -> LanguageDetectionResult:
    """Локально определяет язык текста среди RU / EN / ES без использования LLM.

    Возвращает LanguageDetectionResult со статусом:
    - 'ok' (и код языка 'ru' | 'en' | 'es'),
    - 'ambiguous' (если текст неоднозначен или слишком короткий без явных признаков),
    - 'unsupported' (если обнаружен другой язык/алфавит),
    - 'empty' (если в тексте нет букв).
    """
    cleaned = re.sub(r"https?://\S+|www\.\S+", " ", text or "")
    cleaned = re.sub(r"@[A-Za-z0-9_]+", " ", cleaned)

    letters = [ch for ch in cleaned if ch.isalpha()]
    if not letters:
        return LanguageDetectionResult(status="empty", lang=None, confidence=0.0)

    # Проверка на специфические символы неподдерживаемых языков
    if any(ch in _NON_RUSSIAN_CYRILLIC for ch in letters):
        return LanguageDetectionResult(status="unsupported", lang=None, confidence=0.95)

    if any(ch in _NON_ES_EN_LATIN_CHARS for ch in letters):
        return LanguageDetectionResult(status="unsupported", lang=None, confidence=0.95)

    # Узбекские апострофы o‘ / g‘ / oʻ / gʻ
    if re.search(r"[ogOG][ʻ‘’']", cleaned) and re.search(r"\b(o‘|g‘|oʻ|gʻ|bo‘|ko‘|to‘)", cleaned.lower()):
        return LanguageDetectionResult(status="unsupported", lang=None, confidence=0.95)

    cyrillic_count = 0
    latin_count = 0
    other_script_count = 0

    for ch in letters:
        code = ord(ch)
        if 0x0400 <= code <= 0x052F:
            cyrillic_count += 1
        elif (0x0041 <= code <= 0x007A) or (0x00C0 <= code <= 0x024F):
            latin_count += 1
        else:
            other_script_count += 1

    total_letters = len(letters)
    if other_script_count / total_letters > 0.2:
        return LanguageDetectionResult(status="unsupported", lang=None, confidence=0.95)

    # Если есть и кириллица, и латиница:
    if cyrillic_count > 0 and latin_count > 0:
        cyr_ratio = cyrillic_count / total_letters
        lat_ratio = latin_count / total_letters
        if cyr_ratio >= 0.75:
            return LanguageDetectionResult(status="ok", lang="ru", confidence=cyr_ratio)
        if lat_ratio < 0.75:
            return LanguageDetectionResult(status="ambiguous", lang=None, confidence=0.4)

    # Чистая (или доминирующая) кириллица -> русский
    if cyrillic_count > 0 and latin_count == 0:
        if cyrillic_count < 2:
            return LanguageDetectionResult(status="ambiguous", lang=None, confidence=0.3)
        return LanguageDetectionResult(status="ok", lang="ru", confidence=0.99)

    # Латиница: различаем EN, ES, неподдерживаемые латинские языки и неоднозначные слова
    raw_tokens = re.findall(r"[A-Za-zÁÉÍÓÚÜÑáéíóúüñ']+", cleaned.lower())
    tokens = [t.strip("'") for t in raw_tokens if t.strip("'")]
    if not tokens:
        return LanguageDetectionResult(status="empty", lang=None, confidence=0.0)

    has_spanish_chars = any(ch in _SPANISH_SPECIFIC_CHARS for ch in cleaned)

    # Одно очень короткое слово без испанской диакритики
    if len(tokens) == 1 and not has_spanish_chars:
        word = tokens[0]
        if word in _AMBIGUOUS_COGNATES or len(word) < 2:
            return LanguageDetectionResult(status="ambiguous", lang=None, confidence=0.3)
        if word in _FOREIGN_LATIN_WORDS and word not in _SPANISH_WORDS and word not in _ENGLISH_WORDS:
            return LanguageDetectionResult(status="unsupported", lang=None, confidence=0.85)
        if word in _SPANISH_WORDS and word not in _ENGLISH_WORDS:
            return LanguageDetectionResult(status="ok", lang="es", confidence=0.9)
        if word in _ENGLISH_WORDS and word not in _SPANISH_WORDS:
            return LanguageDetectionResult(status="ok", lang="en", confidence=0.9)
        # Попробуем морфологические суффиксы для одного длинного слова
        if len(word) >= 5:
            if word.endswith(("ción", "sión", "mente", "ando", "iendo", "imiento")):
                return LanguageDetectionResult(status="ok", lang="es", confidence=0.75)
            if word.endswith(("ing", "ght", "ness", "ship", "fully", "lessly")) or "th" in word or "sh" in word:
                return LanguageDetectionResult(status="ok", lang="en", confidence=0.75)
        return LanguageDetectionResult(status="ambiguous", lang=None, confidence=0.35)

    es_score = 0.0
    en_score = 0.0
    foreign_score = 0.0

    if has_spanish_chars:
        es_score += 3.5

    for raw_tok, tok in zip(raw_tokens, tokens):
        # Английские стяжения (don't, I'm, we're, it's, you've, can't)
        if re.search(r"(n't|'re|'ve|'ll|'m|'d|'s)$", raw_tok):
            en_score += 2.5

        if tok in _AMBIGUOUS_COGNATES:
            continue

        in_es = tok in _SPANISH_WORDS
        in_en = tok in _ENGLISH_WORDS
        in_foreign = tok in _FOREIGN_LATIN_WORDS

        if in_es and not in_en:
            es_score += 2.0
        elif in_en and not in_es:
            en_score += 2.0
        elif in_foreign and not in_es and not in_en:
            foreign_score += 2.2

        # Морфологические и орфографические признаки
        if tok.endswith(("ción", "sión", "ciones", "siones", "dad", "dades", "mente", "ando", "iendo", "ábamos", "ieron", "aron")):
            es_score += 1.2
        if tok.endswith(("ing", "ed", "ly", "ness", "ful", "less", "tion", "ight", "ough")):
            en_score += 1.0
        if any(dig in tok for dig in ("th", "sh", "wh", "gh", "ck", "ph", "wr", "kn")):
            en_score += 0.9
        if any(dig in tok for dig in ("ll", "rr", "ch", "qu", "gü", "ue", "ie")):
            es_score += 0.6

    if foreign_score >= 2.2 and foreign_score > es_score and foreign_score > en_score:
        return LanguageDetectionResult(status="unsupported", lang=None, confidence=0.85)

    total_score = es_score + en_score
    if total_score < 1.2:
        return LanguageDetectionResult(status="ambiguous", lang=None, confidence=0.3)

    diff = abs(es_score - en_score)
    confidence = max(es_score, en_score) / (total_score + 0.5)

    if diff < 0.9 or confidence < 0.55:
        return LanguageDetectionResult(status="ambiguous", lang=None, confidence=confidence)

    winner = "es" if es_score > en_score else "en"
    return LanguageDetectionResult(status="ok", lang=winner, confidence=min(0.99, confidence))


# --- РАЗБОР КОМАНД И УПОМИНАНИЙ ---
@dataclass(frozen=True)
class TranslationCommand:
    use_reply_target: bool
    text: str
    explicit_lang: Optional[str] = None
    unsupported_explicit_lang: Optional[str] = None


_EXPLICIT_LANG_PATTERNS = [
    # lang=es, from=ru, src=en, source:es
    re.compile(r"^(?:--?)?(?:lang|from|src|source)\s*[:=]\s*([a-zA-Zа-яА-ЯёЁ\-]+)\s*(.*)$", re.DOTALL | re.IGNORECASE),
    # --from es <text> / -l es <text>
    re.compile(r"^(?:--?(?:lang|from|src)|-l)\s+([a-zA-Zа-яА-ЯёЁ\-]+)(?:\s+(.*))?$", re.DOTALL | re.IGNORECASE),
    # [es] <text> или (es) <text>
    re.compile(r"^[\[\(]\s*([a-zA-Zа-яА-ЯёЁ\-]{2,12})\s*[\]\)]\s*:?\s*(.*)$", re.DOTALL | re.IGNORECASE),
    # es: <text> / ru: <text> / испанский: <text>
    re.compile(r"^([a-zA-Zа-яА-ЯёЁ\-]{2,12})\s*:\s*(.+)$", re.DOTALL | re.IGNORECASE),
    # с испанского[:] [<text>] / from spanish[:] [<text>]
    re.compile(r"^(?:с|со|from)\s+([a-zA-Zа-яА-ЯёЁ\-]{2,15})(?:(?:\s*:\s*|\s+)(.*))?$", re.DOTALL | re.IGNORECASE),
]


def _resolve_lang_token(token: str) -> Tuple[Optional[str], Optional[str]]:
    """Преобразует токен языка в ('ru'|'en'|'es', None) или (None, raw_token) для неподдерживаемого."""
    norm = (token or "").strip().lower().rstrip(":.")
    if norm in LANG_ALIASES:
        return LANG_ALIASES[norm], None
    if re.fullmatch(r"[a-z]{2,3}", norm) and norm in {
        "fr", "de", "it", "pt", "uz", "ar", "zh", "ja", "ko", "tr", "pl", "uk", "be", "nl", "sv",
    }:
        return None, norm
    return None, None


def extract_explicit_lang_and_text(raw_body: str, has_reply: bool = False) -> Tuple[Optional[str], Optional[str], str]:
    """Извлекает явный код исходного языка (если указан) и оставшийся текст для перевода.

    Возвращает кортеж: (explicit_lang, unsupported_explicit_lang, remaining_text).
    """
    body = (raw_body or "").strip()
    if not body:
        return None, None, ""

    # 1. Явные шаблоны с разделителями (lang=es, [es], es:, с испанского ...)
    for pattern in _EXPLICIT_LANG_PATTERNS:
        match = pattern.match(body)
        if match:
            lang_token, rest = match.group(1), (match.group(2) or "").strip()
            resolved, unsupported = _resolve_lang_token(lang_token)
            if resolved or unsupported:
                return resolved, unsupported, rest

    # 2. Если весь текст состоит ровно из одного слова-языка (например, "/translate es" в ответ на сообщение)
    single_resolved, single_unsupported = _resolve_lang_token(body)
    if single_resolved or single_unsupported:
        return single_resolved, single_unsupported, ""

    # 3. Первый токен перед текстом: "/translate es Hola mundo"
    parts = body.split(maxsplit=1)
    if len(parts) == 2:
        first_token, rest = parts[0], parts[1].strip()
        resolved, unsupported = _resolve_lang_token(first_token)
        if unsupported:
            return None, unsupported, rest
        if resolved:
            raw_first = first_token.strip()
            rest_words = re.findall(r"[A-Za-zÁÉÍÓÚÜÑáéíóúüñ']+", rest.lower())
            first_rest_word = rest_words[0] if rest_words else ""

            # Защита от превращения испанского глагола "Es ..." или предлога "En ..." в код языка
            if raw_first.lower() == "es" and first_rest_word in _SPANISH_POST_ES_WORDS and len(rest_words) >= 2:
                return None, None, body
            if raw_first.lower() == "en" and first_rest_word in _SPANISH_POST_EN_WORDS and len(rest_words) >= 2:
                return None, None, body

            return resolved, None, rest

    return None, None, body


_TRANSLATE_TRIGGER_RE = re.compile(
    r"^(?:переведи(?:те)?|перевод|translate|traduce|traducir)\b[,\s:]*(.*)$",
    re.DOTALL | re.IGNORECASE,
)


def parse_message_trigger(
    message: dict,
    bot_username: str,
    bot_id: Optional[int] = None,
) -> Optional[TranslationCommand]:
    """Определяет, требует ли сообщение перевода, и возвращает TranslationCommand либо None."""
    text = (message.get("text") or message.get("caption") or "").strip()
    if not text:
        return None

    reply_msg = message.get("reply_to_message")
    has_reply = isinstance(reply_msg, dict)

    # Проверяем, является ли исходное сообщение (на которое ответили) сообщением самого бота
    reply_from = (reply_msg or {}).get("from") or {}
    reply_from_username = (reply_from.get("username") or "").lstrip("@").lower()
    reply_is_from_this_bot = bool(
        has_reply
        and (
            (bot_id is not None and reply_from.get("id") == bot_id)
            or (bot_username and reply_from_username == bot_username.lower())
        )
    )

    uname_escaped = re.escape(bot_username.lstrip("@"))

    # 1. Команда /translate или /translate@bot_username
    cmd_match = re.match(
        rf"^/translate(?:@{uname_escaped})?(?:\s+(.*))?$",
        text,
        flags=re.DOTALL | re.IGNORECASE,
    )
    if cmd_match:
        raw_arg = (cmd_match.group(1) or "").strip()
        # Поддержка "/translate переведи с испанского"
        trig_match = _TRANSLATE_TRIGGER_RE.match(raw_arg)
        if trig_match and has_reply:
            after_trig = trig_match.group(1).strip()
            explicit_lang, unsupported_lang, remaining = extract_explicit_lang_and_text(
                after_trig, has_reply=True
            )
            if not remaining:
                return TranslationCommand(
                    use_reply_target=True,
                    text="",
                    explicit_lang=explicit_lang,
                    unsupported_explicit_lang=unsupported_lang,
                )

        explicit_lang, unsupported_lang, remaining = extract_explicit_lang_and_text(
            raw_arg, has_reply=has_reply
        )
        if not remaining and has_reply:
            return TranslationCommand(
                use_reply_target=True,
                text="",
                explicit_lang=explicit_lang,
                unsupported_explicit_lang=unsupported_lang,
            )
        return TranslationCommand(
            use_reply_target=False,
            text=remaining,
            explicit_lang=explicit_lang,
            unsupported_explicit_lang=unsupported_lang,
        )

    # Игнорируем другие слэш-команды (или команды, адресованные чужому боту)
    if text.startswith("/"):
        return None

    # 2. Проверка явного упоминания @bot_username
    mention_pattern = re.compile(rf"(?<!\w)@{uname_escaped}\b", re.IGNORECASE)
    if not mention_pattern.search(text):
        # Правило 5 и 6: ответ на сообщение бота без явной команды и прочую переписку игнорируем
        return None

    # Удаляем упоминание бота из текста
    without_mention = mention_pattern.sub(" ", text)
    without_mention = re.sub(r"^\s*[,:\-—]+\s*", "", without_mention).strip()

    # Проверяем, начинается ли обращение со слова «переведи» / «translate» / «traduce»
    trig_match = _TRANSLATE_TRIGGER_RE.match(without_mention)
    if trig_match:
        after_trig = trig_match.group(1).strip()
        explicit_lang, unsupported_lang, remaining = extract_explicit_lang_and_text(
            after_trig, has_reply=has_reply
        )
        if not remaining and has_reply:
            return TranslationCommand(
                use_reply_target=True,
                text="",
                explicit_lang=explicit_lang,
                unsupported_explicit_lang=unsupported_lang,
            )
        if remaining:
            return TranslationCommand(
                use_reply_target=False,
                text=remaining,
                explicit_lang=explicit_lang,
                unsupported_explicit_lang=unsupported_lang,
            )
        # Написали просто "@бот переведи" без ответа на сообщение и без текста
        return TranslationCommand(
            use_reply_target=False,
            text="",
            explicit_lang=explicit_lang,
            unsupported_explicit_lang=unsupported_lang,
        )

    # Правило 5: если это ответ на сообщение самого бота, то перевод исходного сообщения бота
    # выполняется только при явной команде (/translate или «@бот переведи»)
    if reply_is_from_this_bot and not without_mention:
        return None

    explicit_lang, unsupported_lang, remaining = extract_explicit_lang_and_text(
        without_mention, has_reply=has_reply
    )
    if not remaining and has_reply and not reply_is_from_this_bot:
        return TranslationCommand(
            use_reply_target=True,
            text="",
            explicit_lang=explicit_lang,
            unsupported_explicit_lang=unsupported_lang,
        )

    return TranslationCommand(
        use_reply_target=False,
        text=remaining,
        explicit_lang=explicit_lang,
        unsupported_explicit_lang=unsupported_lang,
    )


# --- ФОРМАТИРОВАНИЕ И РАЗБИЕНИЕ ДЛИННЫХ СООБЩЕНИЙ ---
def format_translations(source_lang: str, translations: Dict[str, str]) -> str:
    """Форматирует переводы на два целевых языка с подписями названий языков."""
    targets = TARGET_LANGS_FOR_SOURCE[source_lang]
    blocks = []
    for target in targets:
        label = LANG_LABELS[target]
        translated_text = (translations.get(target) or "").strip()
        blocks.append(f"{label}:\n{translated_text}")
    return "\n\n".join(blocks)


def split_telegram_message(text: str, limit: int = TELEGRAM_MESSAGE_LIMIT) -> List[str]:
    """Разбивает длинный текст на части не длиннее лимита Telegram (4096 символов)."""
    if not text:
        return [""]
    if len(text) <= limit:
        return [text]

    chunks: List[str] = []
    remaining = text
    while remaining:
        if len(remaining) <= limit:
            chunks.append(remaining)
            break

        split_at = remaining.rfind("\n\n", 0, limit)
        if split_at <= 0:
            split_at = remaining.rfind("\n", 0, limit)
        if split_at <= 0:
            split_at = remaining.rfind(" ", 0, limit)
        if split_at <= 0:
            split_at = limit

        chunk = remaining[:split_at].rstrip()
        if not chunk:
            chunk = remaining[:limit]
            split_at = limit
        chunks.append(chunk)
        remaining = remaining[split_at:].lstrip()

    return chunks


# --- КЭШ, ДЕДУПЛИКАЦИЯ И ОГРАНИЧЕНИЕ ЧАСТОТЫ ---
class BoundedLRUCache:
    """Потокобезопасный LRU-кэш фиксированного размера."""

    def __init__(self, max_size: int = 500):
        self.max_size = max(1, max_size)
        self._store: OrderedDict = OrderedDict()
        self._lock = threading.Lock()

    def get(self, key):
        with self._lock:
            if key not in self._store:
                return None
            self._store.move_to_end(key)
            return self._store[key]

    def put(self, key, value) -> None:
        with self._lock:
            if key in self._store:
                self._store.move_to_end(key)
            self._store[key] = value
            while len(self._store) > self.max_size:
                self._store.popitem(last=False)

    def __contains__(self, key) -> bool:
        with self._lock:
            return key in self._store


class UpdateDeduplicator:
    """Защита от повторной обработки одного и того же update_id."""

    def __init__(self, max_size: int = DEFAULT_DEDUP_CACHE_SIZE):
        self._cache = BoundedLRUCache(max_size=max_size)
        self._lock = threading.Lock()

    def is_duplicate_and_mark(self, update_id: Optional[int]) -> bool:
        if update_id is None:
            return False
        with self._lock:
            if update_id in self._cache:
                return True
            self._cache.put(update_id, True)
            return False


class SlidingWindowRateLimiter:
    """Ограничитель частоты запросов на пользователя (скользящее окно)."""

    def __init__(
        self,
        max_requests: int = DEFAULT_RATE_LIMIT_REQUESTS,
        window_sec: float = DEFAULT_RATE_LIMIT_WINDOW_SEC,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.max_requests = max(1, max_requests)
        self.window_sec = max(1.0, window_sec)
        self._clock = clock
        self._events: Dict[Tuple[int, int], deque] = {}
        self._warned_until: Dict[Tuple[int, int], float] = {}
        self._lock = threading.Lock()

    def check(self, chat_id: int, user_id: int) -> Tuple[bool, bool]:
        """Возвращает (allowed, should_send_warning)."""
        now = self._clock()
        key = (chat_id, user_id)
        with self._lock:
            q = self._events.setdefault(key, deque())
            cutoff = now - self.window_sec
            while q and q[0] <= cutoff:
                q.popleft()
            if len(q) >= self.max_requests:
                warn_until = self._warned_until.get(key, 0.0)
                if now >= warn_until:
                    self._warned_until[key] = now + self.window_sec
                    return False, True
                return False, False
            q.append(now)
            return True, False


# --- КЛИЕНТЫ СЕРВИСОВ (ARGOS, WHISPER, TELEGRAM) ---
class ArgosClient:
    """HTTP-клиент к сервису перевода на stelve (Argos Translate, POST /v1/translate).

    Контракт: {"text", "source", "targets": [1-2 кода]} -> {"translations": {код: текст}},
    авторизация Bearer, ошибки {"error": {"code", "message"}}; 429/503 приходят с Retry-After.
    """

    MAX_RETRY_AFTER_SEC = 10.0

    def __init__(
        self,
        base_url: str,
        timeout: float = 30.0,
        api_key: Optional[str] = None,
        session: Optional[requests.Session] = None,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.base_url = (base_url or "http://127.0.0.1:8770").rstrip("/")
        self.timeout = timeout
        self.api_key = api_key
        self._http = session or requests
        self._sleep = sleep

    @property
    def translate_url(self) -> str:
        if self.base_url.endswith("/v1/translate"):
            return self.base_url
        return f"{self.base_url}/v1/translate"

    def _post(self, payload: dict) -> dict:
        headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
        for attempt in range(2):
            try:
                response = self._http.post(
                    self.translate_url,
                    json=payload,
                    headers=headers,
                    timeout=self.timeout,
                )
            except Exception as exc:
                raise ServiceUnavailableError("Argos translation service request failed") from exc
            status = getattr(response, "status_code", 200)
            if status in (429, 503) and attempt == 0:
                # Сервис занят или ещё грузит модели: одна повторная попытка через Retry-After
                try:
                    delay = float(response.headers.get("Retry-After", "5"))
                except (TypeError, ValueError, AttributeError):
                    delay = 5.0
                self._sleep(min(max(delay, 1.0), self.MAX_RETRY_AFTER_SEC))
                continue
            try:
                response.raise_for_status()
                data = response.json()
            except Exception as exc:
                raise ServiceUnavailableError(
                    f"Argos translation service returned HTTP {status}"
                ) from exc
            if not isinstance(data, dict) or not isinstance(data.get("translations"), dict):
                raise ServiceUnavailableError("Invalid response format from Argos service")
            return data["translations"]
        raise ServiceUnavailableError("Argos translation service is busy")

    def translate_many(self, text: str, source_lang: str, target_langs: Sequence[str]) -> Dict[str, str]:
        """Переводит текст сразу на несколько языков одним запросом (RU<->ES сервис делает через EN)."""
        if source_lang not in SUPPORTED_LANGS or any(t not in SUPPORTED_LANGS for t in target_langs):
            raise ValueError(f"Unsupported language direction: {source_lang} -> {list(target_langs)}")
        result = {source_lang: text} if source_lang in target_langs else {}
        targets = [t for t in target_langs if t != source_lang]
        if not targets:
            return result
        translations = self._post({"text": text, "source": source_lang, "targets": targets})
        for target in targets:
            translated = translations.get(target)
            if not isinstance(translated, str) or not translated.strip():
                raise ServiceUnavailableError("Empty translation returned by Argos service")
            result[target] = translated.strip()
        return result

    def translate(self, text: str, source_lang: str, target_lang: str) -> str:
        return self.translate_many(text, source_lang, [target_lang])[target_lang]

    def translate_to_pair(self, text: str, source_lang: str) -> Dict[str, str]:
        """Переводит текст с исходного языка на два остальных языка из RU/EN/ES одним запросом."""
        return self.translate_many(text, source_lang, TARGET_LANGS_FOR_SOURCE[source_lang])


class WhisperClient:
    """HTTP-клиент к существующему серверному сервису Whisper."""

    def __init__(
        self,
        api_url: str,
        timeout: float = 300.0,
        file_field: Optional[str] = None,
        model: Optional[str] = None,
        session: Optional[requests.Session] = None,
        api_key: Optional[str] = None,
        language: Optional[str] = None,
    ):
        self.api_url = (api_url or "http://127.0.0.1:8765/v1/audio/transcriptions").strip()
        self.timeout = timeout
        self.file_field = file_field
        self.model = model
        self.api_key = api_key
        self.language = language
        self._http = session or requests

    def _resolve_file_field(self) -> str:
        if self.file_field:
            return self.file_field
        if self.api_url.rstrip("/").endswith("/transcriptions"):
            return "file"
        return "audio_file"

    def transcribe(self, audio_path: str) -> str:
        field_name = self._resolve_file_field()
        params = {}
        data = {}
        if field_name == "audio_file":
            params = {"task": "transcribe", "output": "json"}
            if self.language:
                params["language"] = self.language
        else:
            data["response_format"] = "json"
            if self.model:
                data["model"] = self.model
            if self.language:
                data["language"] = self.language
        headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else None

        try:
            with open(audio_path, "rb") as audio_file:
                files = {field_name: (os.path.basename(audio_path), audio_file, "audio/ogg")}
                response = self._http.post(
                    self.api_url,
                    params=params or None,
                    data=data or None,
                    files=files,
                    headers=headers,
                    timeout=self.timeout,
                )
            response.raise_for_status()
        except Exception as exc:
            raise ServiceUnavailableError("Whisper transcription service request failed") from exc

        try:
            payload = response.json()
            if isinstance(payload, dict):
                return str(payload.get("text") or payload.get("transcription") or "").strip()
        except ValueError:
            pass

        return (response.text or "").strip()


class TelegramBotClient:
    """Клиент Telegram Bot API для работы в учебном чате."""

    def __init__(
        self,
        token: str,
        timeout: float = 30.0,
        session: Optional[requests.Session] = None,
    ):
        self.token = token
        self.timeout = timeout
        self._http = session or requests

    @property
    def api_base(self) -> str:
        return f"https://api.telegram.org/bot{self.token}"

    @property
    def file_base(self) -> str:
        return f"https://api.telegram.org/file/bot{self.token}"

    def call(self, method: str, **kwargs):
        kwargs.setdefault("timeout", self.timeout)
        try:
            response = self._http.post(f"{self.api_base}/{method}", **kwargs)
            response.raise_for_status()
            payload = response.json()
        except Exception as exc:
            safe_msg = redact_secrets(str(exc), [self.token])
            raise ServiceUnavailableError(f"Telegram API {method} failed: {safe_msg}") from exc

        if not isinstance(payload, dict) or not payload.get("ok"):
            raise ServiceUnavailableError(f"Telegram API {method} returned ok=false")
        return payload.get("result")

    def get_me(self) -> dict:
        return self.call("getMe")

    def get_updates(self, offset: Optional[int] = None, timeout: int = 25) -> List[dict]:
        params = {
            "timeout": timeout,
            "allowed_updates": ["message"],
        }
        if offset is not None:
            params["offset"] = offset
        result = self.call("getUpdates", json=params, timeout=timeout + 10)
        return result if isinstance(result, list) else []

    def send_text_reply(
        self,
        chat_id: int,
        text: str,
        reply_to_message_id: Optional[int] = None,
    ) -> List[dict]:
        """Отправляет текстовый ответ (при превышении 4096 символов — несколькими частями)."""
        chunks = split_telegram_message(text, limit=TELEGRAM_MESSAGE_LIMIT)
        sent_messages: List[dict] = []
        current_reply_id = reply_to_message_id

        for chunk in chunks:
            payload = {
                "chat_id": chat_id,
                "text": chunk,
                "disable_web_page_preview": True,
            }
            if current_reply_id is not None:
                payload["reply_to_message_id"] = current_reply_id
                payload["allow_sending_without_reply"] = True
            sent = self.call("sendMessage", json=payload)
            if isinstance(sent, dict):
                sent_messages.append(sent)
                if sent.get("message_id") is not None:
                    current_reply_id = sent["message_id"]
        return sent_messages

    def download_voice_to_tempfile(
        self,
        file_id: str,
        max_bytes: int = DEFAULT_MAX_VOICE_SIZE_BYTES,
    ) -> str:
        """Скачивает голосовой файл во временный файл с контролем размера."""
        file_info = self.call("getFile", json={"file_id": file_id})
        if not isinstance(file_info, dict) or not file_info.get("file_path"):
            raise ServiceUnavailableError("Telegram getFile did not return file_path")

        declared_size = file_info.get("file_size")
        if isinstance(declared_size, int) and declared_size > max_bytes:
            raise AudioLimitExceededError("Voice file size exceeds maximum limit")

        file_path = str(file_info["file_path"]).lstrip("/")
        url = f"{self.file_base}/{file_path}"

        fd, temp_path = tempfile.mkstemp(prefix="polyglot_voice_", suffix=".ogg")
        os.close(fd)
        try:
            try:
                response = self._http.get(url, stream=True, timeout=self.timeout)
                response.raise_for_status()
            except Exception as exc:
                safe_msg = redact_secrets(str(exc), [self.token])
                raise ServiceUnavailableError(f"Voice download failed: {safe_msg}") from exc

            total_read = 0
            with open(temp_path, "wb") as out_file:
                if hasattr(response, "iter_content"):
                    for chunk in response.iter_content(chunk_size=65536):
                        if not chunk:
                            continue
                        total_read += len(chunk)
                        if total_read > max_bytes:
                            raise AudioLimitExceededError("Downloaded voice stream exceeds size limit")
                        out_file.write(chunk)
                else:
                    content = getattr(response, "content", b"") or b""
                    if len(content) > max_bytes:
                        raise AudioLimitExceededError("Downloaded voice content exceeds size limit")
                    out_file.write(content)
            return temp_path
        except Exception:
            if os.path.exists(temp_path):
                try:
                    os.remove(temp_path)
                except OSError:
                    pass
            raise


# --- ОСНОВНОЙ ОБРАБОТЧИК ЧАТА ---
class StudyChatAssistant:
    """Обработчик событий учебного чата по испанскому языку."""

    TRANSCRIPTION_PREFIX = "🎙 Расшифровка:\n"

    def __init__(
        self,
        config: AssistantConfig,
        tg_client: Optional[TelegramBotClient] = None,
        argos_client: Optional[ArgosClient] = None,
        whisper_client: Optional[WhisperClient] = None,
        bot_id: Optional[int] = None,
    ):
        self.config = config
        self.tg = tg_client or TelegramBotClient(
            token=config.bot_token,
            timeout=config.telegram_timeout,
        )
        self.argos = argos_client or ArgosClient(
            base_url=config.argos_api_url,
            timeout=config.argos_timeout,
            api_key=config.argos_api_key,
        )
        self.whisper = whisper_client or WhisperClient(
            api_url=config.whisper_api_url,
            timeout=config.whisper_timeout,
            file_field=config.whisper_file_field,
            model=config.whisper_model,
            api_key=config.whisper_api_key,
            language=config.whisper_language,
        )
        self.bot_id = bot_id
        self.deduplicator = UpdateDeduplicator(max_size=config.dedup_cache_size)
        self.transcriptions = BoundedLRUCache(max_size=config.transcription_cache_size)
        self.rate_limiter = SlidingWindowRateLimiter(
            max_requests=config.rate_limit_requests,
            window_sec=config.rate_limit_window_sec,
        )
        self._semaphore = threading.BoundedSemaphore(max(1, config.max_concurrent_tasks))

    def is_allowed_chat(self, chat_id: Optional[int]) -> bool:
        if chat_id is None:
            return False
        if self.config.channel_chat_id:
            try:
                if int(chat_id) == int(self.config.channel_chat_id):
                    return False
            except ValueError:
                pass
        return int(chat_id) in self.config.allowed_chat_ids

    def get_saved_transcription(self, chat_id: int, message_id: int) -> Optional[str]:
        return self.transcriptions.get((int(chat_id), int(message_id)))

    def save_transcription(self, chat_id: int, message_id: int, text: str) -> None:
        if text:
            self.transcriptions.put((int(chat_id), int(message_id)), text)

    def _strip_transcription_prefix(self, text: str) -> str:
        if text.startswith(self.TRANSCRIPTION_PREFIX):
            return text[len(self.TRANSCRIPTION_PREFIX):].strip()
        return text.strip()

    def _transcribe_voice_dict(self, chat_id: int, message_id: int, voice: dict) -> str:
        """Проверяет лимиты аудио, скачивает временный файл, вызывает Whisper и удаляет файл."""
        cached = self.get_saved_transcription(chat_id, message_id)
        if cached:
            return cached

        duration = voice.get("duration")
        if isinstance(duration, (int, float)) and duration > self.config.max_voice_duration_sec:
            raise AudioLimitExceededError(
                f"Голосовое сообщение слишком длинное (максимум {self.config.max_voice_duration_sec} сек.)."
            )

        file_size = voice.get("file_size")
        if isinstance(file_size, int) and file_size > self.config.max_voice_size_bytes:
            max_mb = self.config.max_voice_size_bytes // (1024 * 1024)
            raise AudioLimitExceededError(
                f"Файл голосового сообщения слишком большой (максимум {max_mb} МБ)."
            )

        file_id = voice.get("file_id")
        if not file_id:
            raise ServiceUnavailableError("Voice message has no file_id")

        temp_path = self.tg.download_voice_to_tempfile(
            file_id=file_id,
            max_bytes=self.config.max_voice_size_bytes,
        )
        try:
            text = self.whisper.transcribe(temp_path)
        finally:
            if temp_path and os.path.exists(temp_path):
                try:
                    os.remove(temp_path)
                except OSError:
                    logger.warning("Failed to remove temporary voice file")

        if text:
            self.save_transcription(chat_id, message_id, text)
        return text

    def _handle_voice_message(self, chat_id: int, message_id: int, voice: dict) -> str:
        try:
            text = self._transcribe_voice_dict(chat_id, message_id, voice)
        except AudioLimitExceededError as exc:
            self.tg.send_text_reply(chat_id, str(exc), reply_to_message_id=message_id)
            return "voice_limit_exceeded"
        except ServiceUnavailableError:
            logger.error(
                "Whisper transcription failed for chat_id=%s message_id=%s",
                chat_id,
                message_id,
            )
            self.tg.send_text_reply(
                chat_id,
                "Сервис распознавания речи временно недоступен. Пожалуйста, попробуйте позже.",
                reply_to_message_id=message_id,
            )
            return "whisper_error"

        if not text:
            self.tg.send_text_reply(
                chat_id,
                "Не удалось распознать речь в голосовом сообщении.",
                reply_to_message_id=message_id,
            )
            return "voice_empty"

        reply_text = f"{self.TRANSCRIPTION_PREFIX}{text}"
        sent_list = self.tg.send_text_reply(
            chat_id,
            reply_text,
            reply_to_message_id=message_id,
        )
        # Сохраняем расшифровку также по ID сообщения-ответа бота
        for sent in sent_list:
            sent_id = sent.get("message_id")
            if sent_id is not None:
                self.save_transcription(chat_id, sent_id, text)
        return "voice_transcribed"

    def _resolve_reply_target_text(self, chat_id: int, reply_msg: dict) -> Tuple[Optional[str], Optional[str]]:
        """Возвращает (text, error_status) из исходного сообщения, на которое ответили."""
        reply_id = reply_msg.get("message_id")
        if reply_id is not None:
            cached = self.get_saved_transcription(chat_id, reply_id)
            if cached:
                return cached, None

        if isinstance(reply_msg.get("voice"), dict) and reply_id is not None:
            try:
                transcribed = self._transcribe_voice_dict(chat_id, reply_id, reply_msg["voice"])
            except AudioLimitExceededError as exc:
                return None, str(exc)
            except ServiceUnavailableError:
                logger.error(
                    "Whisper transcription on reply failed for chat_id=%s reply_id=%s",
                    chat_id,
                    reply_id,
                )
                return (
                    None,
                    "Сервис распознавания речи временно недоступен. Пожалуйста, попробуйте позже.",
                )
            if not transcribed:
                return None, "Не удалось распознать речь в голосовом сообщении для перевода."
            return transcribed, None

        raw_text = (reply_msg.get("text") or reply_msg.get("caption") or "").strip()
        if raw_text:
            return self._strip_transcription_prefix(raw_text), None

        return None, "В исходном сообщении нет текста или голосового для перевода."

    def _handle_translation_command(
        self,
        chat_id: int,
        message_id: int,
        message: dict,
        cmd: TranslationCommand,
    ) -> str:
        if cmd.unsupported_explicit_lang:
            self.tg.send_text_reply(
                chat_id,
                "Этот язык не поддерживается. Доступны только русский (ru), английский (en) и испанский (es).",
                reply_to_message_id=message_id,
            )
            return "unsupported_language"

        reply_to_id = message_id
        target_text = cmd.text

        if cmd.use_reply_target:
            reply_msg = message.get("reply_to_message") or {}
            reply_to_id = reply_msg.get("message_id") or message_id
            resolved_text, err_msg = self._resolve_reply_target_text(chat_id, reply_msg)
            if err_msg:
                self.tg.send_text_reply(chat_id, err_msg, reply_to_message_id=message_id)
                return "reply_target_error"
            target_text = resolved_text or ""

        target_text = (target_text or "").strip()
        if not target_text:
            self.tg.send_text_reply(
                chat_id,
                f"Укажите текст для перевода или ответьте командой /translate (или «@{self.config.bot_username} переведи») на нужное сообщение.",
                reply_to_message_id=message_id,
            )
            return "empty_request"

        if len(target_text) > self.config.max_text_length:
            self.tg.send_text_reply(
                chat_id,
                f"Текст слишком длинный для разового перевода (максимум {self.config.max_text_length} символов).",
                reply_to_message_id=message_id,
            )
            return "text_too_long"

        # Определение исходного языка
        source_lang = cmd.explicit_lang
        if not source_lang:
            detection = detect_language(target_text)
            if detection.status == "empty":
                self.tg.send_text_reply(
                    chat_id,
                    "Не найден текст для перевода.",
                    reply_to_message_id=message_id,
                )
                return "empty_request"
            if detection.status == "unsupported":
                self.tg.send_text_reply(
                    chat_id,
                    "Этот язык не поддерживается. Перевод работает только между русским (ru), английским (en) и испанским (es). "
                    "Если текст на одном из этих языков, укажите его явно: например, /translate es <текст>.",
                    reply_to_message_id=message_id,
                )
                return "unsupported_language"
            if detection.status == "ambiguous" or not detection.lang:
                self.tg.send_text_reply(
                    chat_id,
                    "Не удалось уверенно определить язык текста. Пожалуйста, укажите исходный язык явно (ru, en или es), "
                    "например:\n/translate es <текст>\nили ответом на сообщение: /translate es",
                    reply_to_message_id=message_id,
                )
                return "ambiguous_language"
            source_lang = detection.lang

        try:
            translations = self.argos.translate_to_pair(target_text, source_lang)
        except ServiceUnavailableError:
            logger.error(
                "Argos translation failed for chat_id=%s message_id=%s source_lang=%s",
                chat_id,
                message_id,
                source_lang,
            )
            self.tg.send_text_reply(
                chat_id,
                "Сервис перевода временно недоступен. Пожалуйста, попробуйте позже.",
                reply_to_message_id=message_id,
            )
            return "argos_error"

        formatted = format_translations(source_lang, translations)
        self.tg.send_text_reply(chat_id, formatted, reply_to_message_id=reply_to_id)
        return f"translated_{source_lang}"

    def handle_update(self, update: dict, executor: Optional[Executor] = None) -> Optional[str]:
        """Обрабатывает одно обновление Telegram. Возвращает код результата или None (если проигнорировано).

        С executor тяжёлая часть (Whisper / Argos) уходит в пул потоков и возвращается "queued",
        чтобы долгое распознавание не задерживало остальные сообщения чата.
        """
        if not isinstance(update, dict):
            return None

        update_id = update.get("update_id")
        if self.deduplicator.is_duplicate_and_mark(update_id):
            logger.info("Skipped duplicate update_id=%s", update_id)
            return "duplicate"

        # Обрабатываем только обычные сообщения (не посты канала и не редактирования)
        message = update.get("message")
        if not isinstance(message, dict):
            return None

        chat = message.get("chat") or {}
        chat_id = chat.get("id")
        if not self.is_allowed_chat(chat_id):
            return None

        sender = message.get("from") or {}
        if sender.get("is_bot"):
            return None

        message_id = message.get("message_id")
        user_id = sender.get("id") or 0
        if message_id is None:
            return None

        # Проверяем, относится ли сообщение к задачам бота (голосовое или команда/упоминание)
        voice = message.get("voice")
        cmd = parse_message_trigger(
            message,
            bot_username=self.config.bot_username,
            bot_id=self.bot_id,
        )

        # Правило 6: всю остальную переписку в учебном чате игнорируем
        if not isinstance(voice, dict) and cmd is None:
            return None

        # Проверяем частоту запросов от пользователя
        allowed, should_warn = self.rate_limiter.check(int(chat_id), int(user_id))
        if not allowed:
            if should_warn:
                self.tg.send_text_reply(
                    int(chat_id),
                    "Слишком много запросов подряд. Пожалуйста, подождите немного.",
                    reply_to_message_id=message_id,
                )
            return "rate_limited"

        # Ограничиваем очередь / параллелизм обработки
        if not self._semaphore.acquire(blocking=False):
            self.tg.send_text_reply(
                int(chat_id),
                "Бот сейчас занят обработкой других сообщений. Пожалуйста, повторите запрос через несколько секунд.",
                reply_to_message_id=message_id,
            )
            return "queue_full"

        if executor is None:
            return self._process(int(chat_id), int(message_id), message, voice, cmd)
        try:
            executor.submit(self._process_logged, int(chat_id), int(message_id), message, voice, cmd)
        except Exception:
            self._semaphore.release()
            raise
        return "queued"

    def _process(self, chat_id: int, message_id: int, message: dict, voice, cmd) -> Optional[str]:
        """Выполняет задачу и освобождает слот семафора, занятый в handle_update."""
        try:
            if isinstance(voice, dict):
                return self._handle_voice_message(chat_id, message_id, voice)
            if cmd is not None:
                return self._handle_translation_command(chat_id, message_id, message, cmd)
            return None
        finally:
            self._semaphore.release()

    def _process_logged(self, chat_id: int, message_id: int, message: dict, voice, cmd) -> None:
        try:
            status = self._process(chat_id, message_id, message, voice, cmd)
            logger.info("chat_id=%s message_id=%s result=%s", chat_id, message_id, status)
        except Exception as exc:
            logger.error(
                "Unhandled error processing chat_id=%s message_id=%s: %s",
                chat_id,
                message_id,
                redact_secrets(f"{type(exc).__name__}: {exc}", [self.config.bot_token]),
            )

    def _touch_heartbeat(self) -> None:
        if not self.config.heartbeat_file:
            return
        try:
            with open(self.config.heartbeat_file, "a"):
                pass
            os.utime(self.config.heartbeat_file, None)
        except OSError:
            logger.warning("Failed to update heartbeat file")

    def run_polling(self, stop_event: Optional[threading.Event] = None) -> None:
        """Запускает long-polling цикл получения обновлений для учебного чата."""
        if not self.config.bot_token:
            raise RuntimeError("TG_BOT_TOKEN is required to run polling")
        if not self.config.allowed_chat_ids:
            raise RuntimeError("ALLOWED_CHAT_IDS is required to run polling safely")

        try:
            me = self.tg.get_me()
            if isinstance(me, dict):
                self.bot_id = me.get("id")
                if me.get("username"):
                    self.config.bot_username = str(me["username"]).lstrip("@")
                logger.info(
                    "Started chat assistant polling as @%s (id=%s) for %d allowed chat(s)",
                    self.config.bot_username,
                    self.bot_id,
                    len(self.config.allowed_chat_ids),
                )
        except ServiceUnavailableError as exc:
            logger.warning("Could not fetch getMe on startup: %s", exc)

        executor = ThreadPoolExecutor(
            max_workers=max(1, self.config.max_concurrent_tasks),
            thread_name_prefix="chat-task",
        )
        offset: Optional[int] = None
        while not (stop_event and stop_event.is_set()):
            self._touch_heartbeat()
            try:
                updates = self.tg.get_updates(
                    offset=offset,
                    timeout=self.config.poll_timeout_sec,
                )
                for upd in updates:
                    upd_id = upd.get("update_id")
                    if isinstance(upd_id, int):
                        offset = upd_id + 1
                    try:
                        self.handle_update(upd, executor=executor)
                    except Exception as exc:
                        logger.error(
                            "Unhandled error processing update_id=%s: %s",
                            upd_id,
                            redact_secrets(str(exc), [self.config.bot_token]),
                        )
            except Exception as exc:
                logger.error(
                    "Polling cycle error: %s",
                    redact_secrets(str(exc), [self.config.bot_token]),
                )
                if stop_event:
                    if stop_event.wait(3.0):
                        break
                else:
                    time.sleep(3.0)
        executor.shutdown(wait=True)


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )
    config = AssistantConfig.from_env()
    if not config.bot_token:
        logger.error("TG_BOT_TOKEN is not set")
        return 1
    if not config.allowed_chat_ids:
        logger.error("ALLOWED_CHAT_IDS (or STUDY_CHAT_ID) is not set; refusing to start")
        return 1

    assistant = StudyChatAssistant(config=config)
    assistant.run_polling()
    return 0


if __name__ == "__main__":
    sys.exit(main())
