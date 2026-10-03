# Contexto para continuar Centinela

Actualizado: 2026-10-03. Rama: `feature/margin-analyst`.

## Preferencias del usuario

- Ejecutar los comandos y verificar resultados; no limitarse a entregarlos.
- Leer README.md y resources/metricas.yaml antes de implementar.
- Mantener monolito sencillo, SQL/Python para cifras y OpenAI para interpretación.
- No modificar SQL oficiales 01/02/03 ni inventar entidades del escenario.
- No hacer commit ni push salvo autorización nueva.
- No mostrar secretos ni leer el .env en salidas de herramientas.

## Estado implementado

S1 — Margen completo en sandbox:
NEW → Analista → ANALYZING → Estratega → PROPOSED → decisión humana →
APPROVED / REJECTED → Ejecutor (solo APPROVED) → EXECUTED.

- MarginDetector y app/vigil conservados tras resolver el merge.
- resources/metricas.yaml es la referencia funcional oficial.
- Analista: investigación SQL read-only acotada al corte de la alerta, SKU,
  costos/precios ponderados, proveedores de catálogo y contribuciones descriptivas.
- RAG: PDF/pypdf, embeddings OpenAI, PostgreSQL pgvector, procedencia por documento,
  página y chunk; indexación idempotente. Documentos tratados como UNTRUSTED DATA.
- Analista conserva ANALYZING al terminar, root_cause y confidence persistidos,
  proposals NULL. root_cause contiene analysis, evidence y policy_references.
- Estratega: usa exclusivamente análisis persistido, sin nueva investigación SQL/RAG.
  Responses API Structured Outputs, entre una y tres propuestas, allowlist cerrado.
- amount_at_risk = suma de contribuciones positivas de SKU, Decimal, total redondeado
  a centavos. Es erosión estimada observada de la semana, nunca ahorro garantizado.
  Si no hay contribuciones comparables: NULL; si todas son negativas: cero.
- financial_reference_cop no está en el esquema del modelo: lo establece el backend.
- Acciones: CREATE_PRICE_REVIEW_DRAFT, CREATE_MARGIN_FOLLOWUP_TASK,
  CREATE_SUPPLIER_REVIEW_TASK. Ningún efecto externo.
- Decisiones existentes reutilizadas: APPROVED/REJECTED cambian estado; EDITED
  reemplaza propuestas y conserva PROPOSED. No se renombraron sus eventos.
- Ejecutor: permisos gerente/líder misma área, únicamente margen COMERCIAL;
  status APPROVED obligatorio en código. Valida propuestas editadas en la barrera.
- Ejecución atómica de acciones, bitácora y estado; rollback conserva APPROVED;
  fallo se registra independientemente cuando es posible. UNIQUE dedupe_key y
  UNIQUE(alert_id,proposal_id). Repetir después de EXECUTED devuelve 409 sin duplicados.
- Estratega usa lock de fila durante OpenAI para evitar llamadas concurrentes duplicadas.
  Fallo conserva ANALYZING/análisis y permite reintento.
- Header temporal X-User-Id, usuario activo. Estrategia: GERENTE/ANALISTA.
  Ejecutar: GERENTE/LIDER_PROCESO de COMERCIAL. Lectura según permisos existentes.

## Archivos nuevos de este último bloque

- app/strategist/__init__.py, service.py, schemas.py
- app/executor/__init__.py, service.py, schemas.py
- app/api/s1.py
- database/sql/08_executor.sql
- database/scripts/apply_executor.py
- scripts/integration_s1_full.py
- tests/test_s1.py, tests/test_s1_postgres.py
- docs/s1_demo.md

Modificados por S1: README.md, app/api/router.py, app/models/core.py,
app/models/__init__.py.

Modificaciones previas conservadas y aún sin commit: app/analyst/service.py,
scripts/integration_margin_openai.py, tests/test_analyst.py.

El diagnóstico de integración tiene --debug, etapa, clase, mensaje y traceback
sanitizados, sin variables locales. El Analista tenía un regex demasiado amplio
que rechazaba disclaimers sobre incumplimiento: ahora normaliza esos enunciados
a una limitación del backend sobre ausencia de calendario, conservando las citas.

## Base de datos y validación realizada

- 08_executor.sql ya aplicado en Neon mediante apply_executor.py.
- Dataset oficial conservado. No se aprobó ni ejecutó ninguna alerta real de producción.
- Suite normal: 78 tests descubiertos, 74 aprobados y 4 PostgreSQL omitidos.
- Suite completa con CENTINELA_TEST_POSTGRES=1: 78 aprobados, sin omisiones.
- PostgreSQL end-to-end: NEW → análisis → ANALYZING → estrategia → PROPOSED →
  decisión existente APPROVED → sandbox → EXECUTED; tres acciones, eventos completos,
  segunda ejecución 409 y sin duplicados.
- E2E PostgreSQL usa evidencia real, OpenAI mockeado y limpia exclusivamente sus
  UUID propios en finally. Verificó cero registros propios restantes.
- Otras pruebas PostgreSQL usan rollback. Tests no consumieron créditos OpenAI.
- Script integration_s1_full validado con mocks desde NEW, ANALYZING y PROPOSED;
  conteo de llamadas y parada previa a aprobación comprobados. No se ejecutó con
  OpenAI real en este último bloque.
- git diff --check pasó, endpoints registrados verificados, .env ignorado/no trackeado,
  escaneo de valores secretos pasó. No commit ni push.

## Comandos

```powershell
.\.venv\Scripts\python.exe database/scripts/apply_executor.py
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
$env:CENTINELA_TEST_POSTGRES = "1"
try {
    .\.venv\Scripts\python.exe -m unittest discover -s tests -v
} finally {
    Remove-Item Env:\CENTINELA_TEST_POSTGRES
}
```

Variables existentes necesarias: DATABASE_URL, DATABASE_URL_UNPOOLED,
OPENAI_API_KEY, OPENAI_MODEL_REASONING, OPENAI_EMBEDDING_MODEL.
No hay nombres de modelo hardcodeados ni nuevas dependencias para Estratega/Ejecutor.

Integración real opcional (consume API, persiste análisis/propuestas, nunca aprueba
ni ejecuta; atribuye eventos al primer gerente activo ordenado por UUID):

```powershell
.\.venv\Scripts\python.exe scripts/integration_s1_full.py --alert-id UUID_DE_ALERTA_REAL --debug
```

El usuario probó antes con la alerta real `1b5aba28-0f7c-454c-ae5a-23551fa57bd7`
el script integration_margin_openai.py. Esa integración terminó correctamente
en modo read-only: no persistió análisis ni cambió la alerta. No asumir su estado
actual; consultarlo antes de actuar.

La demo manual completa, cuerpos y X-User-Id están documentados en docs/s1_demo.md:
GET alerta, POST analizar, POST estrategia, decisión humana explícita,
POST ejecutar, GET ejecuciones y GET bitacora?alert_id=UUID.

## Limitaciones y siguiente sesión

- Solo S1. No implementar otros escenarios sin nueva instrucción.
- Sin LangGraph, MCP, ejecución externa o autenticación empresarial.
- Sin calendario colombiano: no afirmar incumplimiento de días hábiles.
- Las ediciones genéricas del endpoint existente siguen admitidas, pero la ejecución
  requiere el contrato sandbox: objeto summary/proposals, lista o propuesta única.
- Estratega puede mantener un lock hasta el timeout del SDK; no hay colas.
- Al retomar: leer este archivo, README.md y docs/s1_demo.md; ejecutar git status y
  verificar la rama antes de cambiar archivos. Los cambios siguen sin commit.

Estado de Git al terminar S1 (antes de añadir este archivo):

```text
 M README.md
 M app/analyst/service.py
 M app/api/router.py
 M app/models/__init__.py
 M app/models/core.py
 M scripts/integration_margin_openai.py
 M tests/test_analyst.py
?? app/api/s1.py
?? app/executor/
?? app/strategist/
?? database/scripts/apply_executor.py
?? database/sql/08_executor.sql
?? docs/
?? scripts/integration_s1_full.py
?? tests/test_s1.py
?? tests/test_s1_postgres.py
```
