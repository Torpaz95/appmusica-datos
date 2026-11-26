# appmusica-datos

Lista de artistas musicales para la app **App Música**, preparada automáticamente a partir de [Wikidata](https://www.wikidata.org) y actualizada cada semana.

- Los datos de Wikidata son de dominio público (CC0).
- Este repositorio solo contiene el programa que genera la lista, la lista y un informe de calidad. No contiene el código de la app.

## Contenido

- `generar.py`: el programa que consulta Wikidata y aplica las reglas de búsqueda de la app.
- `datos/artistas.tsv.gz`: la lista (columnas: `id`, `tipo` p=persona / g=grupo, `fama` = número de Wikipedias con artículo, `nombre`, `alias` separados por `|`, `anio`, `pais` en código ISO).
- `datos/informe.md`: informe de calidad de la última actualización.
- `.github/workflows/actualizar.yml`: tarea automática que regenera la lista cada lunes.
