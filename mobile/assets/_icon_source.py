"""Momentum Scanner uygulama ikonunu uretir - web uygulamasinin kendi Skor
renk sistemiyle (SAT=kirmizi, NOTR=sari, AL=yesil) birebir ayni renk
gecisini kullanan, buyudukce kazanca donusen 5 mumluk bir motif. Tek
seferlik bir yardimci script - normal proje calismasinin bir parcasi degil,
ikon degistirilmek istendiginde tekrar calistirilir."""
import cairosvg

BG = "#0f1115"
RED = "#ff5c5c"       # web'deki --red (SAT)
YELLOW = "#e8c547"    # web'deki --yellow (NOTR)
GREEN = "#2ecc71"     # web'deki --green (AL)
GREEN_STRONG = "#17c964"  # web'deki --green-strong (GUCLU_AL)

# 5 mum, kucukten buyuge: kirmizi (kayip) -> sari sari (notr) -> yesil yesil
# (kazanc, en buyugu en guclu kazanc). Web uygulamasindaki Skor renkleriyle
# (sym-SAT/sym-NOTR/sym-AL) ayni mantik. Icerik 1024x1024 tuval icinde ~66%
# guvenli bolgede (Android adaptive icon maskelemesi - daire/squircle/
# yuvarlak kare - hangi sekilde kirpilirse kirpilsin kesilmesin diye)
# ortalanmis.
BAR_W = 70
GAP = 25
STEP = BAR_W + GAP
HEIGHTS = [140, 230, 320, 410, 500]
COLORS = [RED, YELLOW, YELLOW, GREEN, GREEN_STRONG]
N = len(HEIGHTS)
TOTAL_W = N * BAR_W + (N - 1) * GAP
START_X = (1024 - TOTAL_W) / 2
BARS = [(START_X + i * STEP, HEIGHTS[i], COLORS[i]) for i in range(N)]

BASELINE = 760
WICK_W = 10
WICK_OVERHANG = 40


def candlestick_glyph_svg(monochrome: bool = False) -> str:
    shapes = []
    for x, h, color in BARS:
        fill = "#ffffff" if monochrome else color
        top = BASELINE - h
        cx = x + BAR_W / 2
        wick_top = top - WICK_OVERHANG
        wick_bottom = BASELINE + WICK_OVERHANG
        shapes.append(
            f'<line x1="{cx}" y1="{wick_top}" x2="{cx}" y2="{wick_bottom}" '
            f'stroke="{fill}" stroke-width="{WICK_W}" stroke-linecap="round"/>'
        )
        shapes.append(
            f'<rect x="{x}" y="{top}" width="{BAR_W}" height="{h}" rx="10" fill="{fill}"/>'
        )
    return "\n".join(shapes)


def build_svg(with_background: bool, monochrome: bool) -> str:
    bg_rect = f'<rect width="1024" height="1024" fill="{BG}"/>' if with_background else ""
    glyph = candlestick_glyph_svg(monochrome=monochrome)
    return f'''<svg xmlns="http://www.w3.org/2000/svg" width="1024" height="1024" viewBox="0 0 1024 1024">
  {bg_rect}
  {glyph}
</svg>'''


def render(svg_str: str, out_path: str, size: int = 1024):
    cairosvg.svg2png(bytestring=svg_str.encode("utf-8"), write_to=out_path, output_width=size, output_height=size)


if __name__ == "__main__":
    # icon.png - tek parca (arka plan + glyph birlikte), genel/iOS ikonu
    render(build_svg(with_background=True, monochrome=False), "icon.png")

    # Android adaptive icon - iki ayri katman
    render(build_svg(with_background=False, monochrome=False), "android-icon-foreground.png")
    render(f'<svg xmlns="http://www.w3.org/2000/svg" width="1024" height="1024"><rect width="1024" height="1024" fill="{BG}"/></svg>', "android-icon-background.png")
    render(build_svg(with_background=False, monochrome=True), "android-icon-monochrome.png")

    # favicon - kucuk boyut, arka planli
    render(build_svg(with_background=True, monochrome=False), "favicon.png", size=196)

    # splash-icon - transparan, splash ekraninda ortada gosterilir
    render(build_svg(with_background=False, monochrome=False), "splash-icon.png")

    print("Tum ikon dosyalari olusturuldu.")
