# S1 — Margen: estrategia, decisión y ejecución sandbox

El flujo implementado es NEW → ANALYZING (análisis terminado) → PROPOSED →
APPROVED o REJECTED → EXECUTED para las aprobadas. EDITED conserva PROPOSED y
requiere una decisión posterior. Se reutiliza el endpoint de decisión existente.
El detector sigue la referencia funcional resources/metricas.yaml; no se modifican
sus reglas, los otros escenarios ni las tablas oficiales centinela.*.

El Estratega usa únicamente root_cause, evidencia persistida, referencias de
política y confidence. No vuelve a consultar el dataset ni genera embeddings.
OpenAI Responses API produce de una a tres propuestas con enum cerrado y sin
financial_reference_cop en el esquema del modelo. Python suma contribuciones
positivas de SKU y redondea solo el total a centavos (ROUND_HALF_UP, Numeric 18,2).
El backend establece financial_reference_cop después de validar la respuesta.
Sin contribuciones comparables el importe es NULL; si todas son negativas, cero.
El importe es erosión estimada observada de la semana frente al histórico, no
ahorro, recuperación garantizada ni predicción futura. Las cifras permanecen en
los campos determinísticos; la prosa del modelo es cualitativa.

Las únicas acciones son CREATE_PRICE_REVIEW_DRAFT, CREATE_MARGIN_FOLLOWUP_TASK y
CREATE_SUPPLIER_REVIEW_TASK. La política recuperada se trata como UNTRUSTED DATA;
no se obedecen instrucciones documentales, no se generan SQL ni se ejecutan
herramientas. No se evalúa el plazo de días hábiles.

La estrategia bloquea la alerta durante la solicitud OpenAI para evitar consumo
duplicado concurrente. STARTED, COMPLETED, propuestas y estado se confirman juntos.
Si falla, rollback conserva ANALYZING, root_cause y evidencia; una transacción
independiente registra STARTED/FAILED cuando es posible. Puede reintentarse.
El lock puede durar hasta el timeout de OpenAI; no hay cola ni recuperación de
procesos en segundo plano en este bloque.

El Ejecutor no usa OpenAI. Verifica APPROVED en código, la misma área para líderes,
tipo MARGIN_ANOMALY y área COMERCIAL. Valida también las propuestas editadas antes
de ejecutarlas; una edición con otro formato sigue permitida por el endpoint
existente pero no puede ejecutarse hasta ajustarse al contrato sandbox (422).
Admite el objeto summary/proposals, una lista de propuestas o una propuesta única.
Los importes editados no sustituyen amount_at_risk. Los SKU del borrador salen solo
de contribuciones positivas de la investigación persistida, nunca del modelo.

Todas las acciones, EXECUTION_STARTED, EXECUTION_COMPLETED y EXECUTED se guardan en
una transacción. Un fallo revierte acciones y estado; una transacción independiente
registra STARTED/FAILED y conserva APPROVED para reintentar. Se usan locks de fila,
dedupe_key UNIQUE y UNIQUE(alert_id,proposal_id). Repetir después de EXECUTED devuelve
409 sin nuevas acciones ni eventos, respetando la barrera APPROVED. No se envían
correos, no se cambian precios, pedidos o sistemas externos.

## Configuración y aplicación

Variables del .env existente:

```env
DATABASE_URL=...
DATABASE_URL_UNPOOLED=...
OPENAI_API_KEY=...
OPENAI_MODEL_REASONING=...
OPENAI_EMBEDDING_MODEL=...
```

No hay modelos predeterminados ni nuevas dependencias. Mantener las políticas
indexadas con el modelo de embeddings configurado. Aplicar únicamente el nuevo SQL:

```powershell
.\.venv\Scripts\python.exe database/scripts/apply_executor.py
```

Ejecuta database/sql/08_executor.sql idempotentemente, usando conexión administrativa.
No reconstruye el dataset ni crea alertas de muestra. Rutas con pathlib.Path.

## Pruebas

Suite normal sin créditos OpenAI:

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

Pruebas PostgreSQL relevantes, incluyendo S1 end-to-end:

```powershell
$env:CENTINELA_TEST_POSTGRES = "1"
try {
    .\.venv\Scripts\python.exe -m unittest discover -s tests -p '*postgres.py' -v
    .\.venv\Scripts\python.exe -m unittest discover -s tests -p test_decisions.py -v
} finally {
    Remove-Item Env:\CENTINELA_TEST_POSTGRES
}
```

test_s1_postgres usa datos oficiales y OpenAI mockeado, crea exclusivamente UUID
propios y borra sus acciones, decisiones, bitácora, alerta y usuario en finally.
Verifica cero registros propios restantes. Las otras pruebas usan rollback.

Integración **real opcional**, consume API y persiste análisis/propuestas sobre
una alerta existente; se detiene antes de la decisión humana:

```powershell
.\.venv\Scripts\python.exe scripts/integration_s1_full.py --alert-id UUID_DE_ALERTA_REAL --debug
```

Si NEW, ejecuta Analista real (SQL, embeddings RAG y razonamiento), luego Estratega.
Si ANALYZING, solo genera estrategia tras validar análisis completo. Si PROPOSED,
muestra propuestas sin volver a consumir OpenAI. Imprime estado, presencia de causa,
importe, propuestas y conteo de solicitudes intentadas (razonamiento y embeddings,
sin retries automáticos). Atribuye los eventos al primer gerente activo ordenado
por UUID. No aprueba ni ejecuta. Errores y traceback sanitizados; no expone claves,
URLs de base, Authorization ni variables locales. No pertenece a la suite normal.

## Demo mediante API

Iniciar el backend en otra terminal:

```powershell
.\.venv\Scripts\python.exe -m uvicorn app.main:app --reload
```

Usar una alerta real NEW. Si ya tiene análisis o propuestas, omitir el paso ya
completado. Todos los endpoints del flujo y las consultas requieren X-User-Id;
GET /users/demo permite seleccionar el gerente y no requiere ese header.

```powershell
$centinelaBase = "http://127.0.0.1:8000"
$centinelaAlertId = "UUID_DE_ALERTA_REAL"
$centinelaUsers = Invoke-RestMethod "$centinelaBase/users/demo"
$centinelaManager = $centinelaUsers | Where-Object { $_.role -eq "GERENTE" -and $_.active } | Select-Object -First 1
$centinelaHeaders = @{ "X-User-Id" = $centinelaManager.id }

Invoke-RestMethod "$centinelaBase/alertas/$centinelaAlertId" -Headers $centinelaHeaders
Invoke-RestMethod "$centinelaBase/alertas/$centinelaAlertId/analizar" -Method Post -Headers $centinelaHeaders
Invoke-RestMethod "$centinelaBase/alertas/$centinelaAlertId/estrategia" -Method Post -Headers $centinelaHeaders
```

Inspeccionar las propuestas y tomar la decisión humana explícita. Para aprobar:

```powershell
$centinelaDecision = @{ decision = "APPROVED"; reason = "Aprobación humana para sandbox"; edited_proposal = $null } | ConvertTo-Json
Invoke-RestMethod "$centinelaBase/alertas/$centinelaAlertId/decision" -Method Post -Headers $centinelaHeaders -ContentType "application/json" -Body $centinelaDecision
Invoke-RestMethod "$centinelaBase/alertas/$centinelaAlertId/ejecutar" -Method Post -Headers $centinelaHeaders
Invoke-RestMethod "$centinelaBase/alertas/$centinelaAlertId/ejecuciones" -Headers $centinelaHeaders
Invoke-RestMethod "$centinelaBase/bitacora?alert_id=$centinelaAlertId" -Headers $centinelaHeaders
```

Para rechazar, usar decision=REJECTED; la ejecución devuelve 409. Para editar,
usar decision=EDITED con edited_proposal como objeto summary/proposals validado
(mismos campos de cada propuesta); sigue PROPOSED hasta aprobar o rechazar.
Gerente y Analista generan estrategia. Gerente y líder COMERCIAL pueden decidir
y ejecutar. Analista/Auditor solo consultan ejecuciones, sin ejecutarlas. Un líder
de otra área recibe 403 en detalle/ejecución; bitácora mantiene el filtro existente.

Secuencia de bitácora esperada:
ALERT_DETECTED, ANALYSIS_STARTED, ANALYSIS_COMPLETED, STRATEGY_STARTED,
STRATEGY_COMPLETED, ALERT_DECISION_APPROVED, EXECUTION_STARTED, EXECUTION_COMPLETED.

La autenticación sigue siendo la identificación temporal X-User-Id. El Ejecutor
solo produce artefactos sandbox; no está implementada ejecución externa.
