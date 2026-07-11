_TURKISH_MOJIBAKE = str.maketrans({
    'ý': 'ı', 'ð': 'ğ', 'þ': 'ş',
    'Ý': 'İ', 'Ð': 'Ğ', 'Þ': 'Ş',
})


def repair_turkish_text(text: str) -> str:
    return text.translate(_TURKISH_MOJIBAKE)
