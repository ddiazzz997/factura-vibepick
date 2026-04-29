#!/usr/bin/env bash
# Render factura.html → factura.pdf usando Chrome headless.
# Falla si el PDF resultante tiene más de una página.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
HTML="$HERE/factura.html"
PDF="$HERE/factura.pdf"
CHROME="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"

[[ -f "$HTML" ]] || { echo "✗ No existe $HTML" >&2; exit 2; }
[[ -x "$CHROME" ]] || { echo "✗ Chrome no encontrado en $CHROME" >&2; exit 2; }

echo "→ Renderizando PDF..."
"$CHROME" \
  --headless \
  --disable-gpu \
  --no-pdf-header-footer \
  --print-to-pdf="$PDF" \
  --print-to-pdf-no-header \
  --no-margins \
  --virtual-time-budget=4000 \
  "file://$HTML" 2>/dev/null

[[ -f "$PDF" ]] || { echo "✗ Chrome no generó el PDF" >&2; exit 1; }

# Verificación dura: una sola página
PAGES=$(pdfinfo "$PDF" | awk '/^Pages:/ {print $2}')
if [[ "$PAGES" != "1" ]]; then
  echo "✗ El PDF tiene $PAGES páginas. La factura DEBE caber en una." >&2
  exit 1
fi

SIZE=$(du -h "$PDF" | cut -f1)
echo "✓ PDF generado: $PDF ($SIZE, $PAGES página)"
