# S3 — Inventario crítico: detección, análisis, estrategia y ejecución sandbox

Reutiliza la arquitectura de S1: mismo Vigía, Analista, RAG, Estratega, endpoint de
decisión, Ejecutor, `app.execution_actions` y bitácora. No hay endpoints nuevos:

```http
POST /vigia/ejecutar                 POST /simulacion/avanzar?dias=N
POST /alertas/{id}/analizar          POST /alertas/{id}/estrategia
POST /alertas/{id}/decision          POST /alertas/{id}/ejecutar
GET  /alertas/{id}/ejecuciones       GET  /bitacora
```

Analizar, Estratega y Ejecutor despachan por `alert.type` mediante un registro de
escenarios (`MARGIN_ANOMALY`, `INVENTORY_RISK`). S1 conserva reglas, prompts y contratos.

## Detector (`app/vigil/detectors/inventory.py`)

Fuente: `resources/metricas.yaml → cobertura_dias` sobre `centinela.v_cobertura_inventario`
(ya acotada por `fecha_corte()`), más los mínimos de la política OPE-POL-007.

| Regla | Severidad |
|---|---|
| cobertura < 5 días **y** unidades pendientes > 0 | `CRITICAL` |
| clase A < 10, clase B < 7, clase C < 5 días | `HIGH` |

- `type=INVENTORY_RISK`, `area=INVENTARIO`, `status=NEW`. `severity` es texto libre en
  el contrato existente; `CRITICAL` se usa solo para el caso crítico oficial.
- Evidencia: SKU, producto, línea, clase, bodega, corte, existencia, demanda 30 d,
  cobertura, mínimo de clase, pendientes y triggers. Todo sale de la vista.
- `dedupe_key = [INVENTORY_RISK, sku, bodega, severidad, semana del corte]`: re-ejecutar
  no duplica; una semana nueva o la escalada HIGH → CRITICAL generan otra alerta.
- SKU con demanda cero (cobertura NULL) se omiten.

## Analista (`app/analyst/inventory.py`)

SELECT fijos y parametrizados en transacción read-only, con el corte de la alerta:

- `productos` + `proveedores`: clase y proveedor de catálogo.
- `inventario_diario`: ventana actual de 30 días (misma definición que la vista) y
  ventana previa de 60 días → demanda actual, histórica y variación %; entradas y salidas.
  La cobertura se recalcula y se marca si no coincide con el detector.
- `ordenes_compra`: órdenes con `fecha_oc <= corte`. **El estado se deriva de fechas al
  corte** (RECIBIDA / RETRASADA si `fecha_esperada < corte` sin recepción / EN_TRANSITO);
  la columna `estado` del dataset es el estado final y no se usa. Recepciones posteriores
  al corte se ocultan.
- Pedidos pendientes: los de la vista oficial al corte de detección (evidencia del detector).

`insufficient_evidence = true` salvo que SQL respalde al menos un mecanismo (orden
retrasada o aumento de demanda) y la cobertura recalculada coincida con el detector.

Limitación del dataset: no hay cantidad recibida por orden y todas las recepciones
registradas coinciden con lo pedido, así que **no puede demostrarse una entrega parcial**;
el Analista y el Estratega tienen prohibido afirmarla.

RAG: mismo índice (`app.policy_chunks`) con `INVENTORY_POLICY_QUERY`. OpenAI recibe solo
evidencia, catálogo de hechos y fragmentos (UNTRUSTED DATA). El backend rechaza prosa con
cifras, hechos DATA fuera del catálogo y citas no literales.

## Estratega y Ejecutor

Allowlist S3 (el Structured Output solo expone estas; el Ejecutor rechaza otras con 422,
incluidas ediciones humanas con acciones de S1):

| Acción | Resultado sandbox |
|---|---|
| `CREATE_PURCHASE_ORDER_REVIEW_TASK` | `TASK_CREATED` / `PURCHASE_ORDER_REVIEW` |
| `CREATE_SUPPLIER_FOLLOWUP_DRAFT` | `DRAFT_CREATED` / `SUPPLIER_FOLLOWUP`, `sent: false` |
| `CREATE_ALTERNATE_SUPPLIER_REVIEW_TASK` | `TASK_CREATED` / `ALTERNATE_SUPPLIER_REVIEW` |
| `CREATE_URGENT_PARTIAL_DELIVERY_REVIEW` | `TASK_CREATED` / `URGENT_PARTIAL_DELIVERY_REVIEW` |

SKU, bodega, órdenes y proveedor (de la orden retrasada o, si no hay, del catálogo) salen
de la evidencia persistida. Nada crea órdenes, envía correos, contacta proveedores ni
modifica inventario.

`amount_at_risk = NULL`: la política de inventario no define fórmula financiera.

La barrera `status == APPROVED` está en código (409 si no). Permisos: RBAC existente;
el líder de proceso debe ser del área `INVENTARIO`.

## SQL y comandos

`database/sql/09_inventory_scenario.sql` amplía el CHECK de `app.execution_actions.action_type`
(superconjunto idempotente; no toca el dataset). Aplicar una vez:

```powershell
.\.venv\Scripts\python.exe database/scripts/apply_inventory_scenario.py
```

Pruebas (mocks, sin créditos):

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
$env:CENTINELA_TEST_POSTGRES = "1"; .\.venv\Scripts\python.exe -m unittest discover -s tests -p '*postgres.py' -v
```

`test_inventory_postgres` recorre todo el reloj simulado con el Vigía en una transacción
revertida (bloquea la fila del reloj unos minutos) y descubre el caso crítico sin IDs
fijos; el flujo API end-to-end limpia sus propios datos y requiere el SQL 09.

Integración real opcional (consume OpenAI; se detiene antes de la decisión humana):

```powershell
.\.venv\Scripts\python.exe scripts/integration_s3_full.py --alert-id UUID --debug
```
