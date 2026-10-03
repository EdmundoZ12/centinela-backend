-- Centinela · capa semántica: vistas de métricas que consumen los agentes (solo lectura)
-- La fecha de corte por defecto es el último día del dataset. Con el reloj simulado, reemplazar
-- centinela.fecha_corte() por el día simulado.
SET search_path TO centinela;

CREATE OR REPLACE FUNCTION fecha_corte() RETURNS date LANGUAGE sql STABLE AS $$ SELECT max(fecha) FROM inventario_diario $$;

-- Ventas netas con costo y margen por línea de pedido (excluye pedidos cancelados)
CREATE OR REPLACE VIEW v_ventas AS
SELECT p.pedido_id, p.fecha, p.cliente_id, c.nombre AS cliente, c.segmento, p.vendedor_id, p.ciudad, c.region,
       d.linea_n, d.sku, pr.nombre AS producto, pr.linea, d.cantidad, d.precio_lista, d.precio_unitario,
       d.descuento_pct, d.aprobacion_especial, d.valor_neto, d.cantidad * d.costo_unitario AS costo_total,
       d.valor_neto - d.cantidad * d.costo_unitario AS margen_bruto, p.estado
FROM pedidos p JOIN pedidos_detalle d USING (pedido_id) JOIN clientes c USING (cliente_id) JOIN productos pr USING (sku)
WHERE p.estado <> 'Cancelado';

-- Margen semanal por línea
CREATE OR REPLACE VIEW v_margen_semanal_linea AS
SELECT date_trunc('week', fecha)::date AS semana, linea, sum(valor_neto) AS ventas, sum(costo_total) AS costo,
       round(100 * (1 - sum(costo_total) / nullif(sum(valor_neto), 0)), 2) AS margen_pct
FROM v_ventas GROUP BY 1, 2;

-- Cartera por cliente a la fecha de corte
CREATE OR REPLACE VIEW v_cartera_cliente AS
WITH pag AS (SELECT factura_id, sum(valor) AS pagado, max(fecha_pago) AS fecha_pago FROM pagos
             WHERE fecha_pago <= fecha_corte() GROUP BY factura_id),
f AS (SELECT f.*, coalesce(pag.pagado, 0) AS pagado, pag.fecha_pago FROM facturas f LEFT JOIN pag USING (factura_id)
      WHERE f.fecha_factura <= fecha_corte())
SELECT c.cliente_id, c.nombre, c.segmento, c.cupo_credito, c.plazo_dias,
       sum(f.valor_total - f.pagado) AS saldo_abierto,
       sum(CASE WHEN f.fecha_vencimiento < fecha_corte() THEN f.valor_total - f.pagado ELSE 0 END) AS saldo_vencido,
       max(CASE WHEN f.valor_total > f.pagado THEN fecha_corte() - f.fecha_vencimiento END) AS max_dias_vencido,
       round(avg(CASE WHEN f.fecha_pago IS NOT NULL AND f.fecha_factura >= fecha_corte() - 120
                      THEN f.fecha_pago - f.fecha_factura END), 1) AS dias_pago_prom_120d
FROM clientes c JOIN f USING (cliente_id) GROUP BY 1, 2, 3, 4, 5;

-- Días de pago promedio por cliente y mes de factura (tendencia)
CREATE OR REPLACE VIEW v_dias_pago_mensual AS
SELECT f.cliente_id, date_trunc('month', f.fecha_factura)::date AS mes_factura,
       round(avg(p.fecha_pago - f.fecha_factura), 1) AS dias_pago_prom, count(*) AS facturas_pagadas
FROM facturas f JOIN pagos p USING (factura_id) GROUP BY 1, 2;

-- Cobertura de inventario por SKU y bodega a la fecha de corte
CREATE OR REPLACE VIEW v_cobertura_inventario AS
WITH dem AS (SELECT sku, bodega_id, avg(salidas) AS demanda_prom_30d FROM inventario_diario
             WHERE fecha > fecha_corte() - 30 AND fecha <= fecha_corte() GROUP BY 1, 2),
pend AS (SELECT d.sku, b.bodega_id, sum(d.cantidad) AS unidades_pendientes
         FROM pedidos p JOIN pedidos_detalle d USING (pedido_id)
         JOIN (VALUES ('Medellín','BOD-MDE'),('Pereira','BOD-MDE'),('Barranquilla','BOD-MDE'),('Cartagena','BOD-MDE'),
                      ('Bogotá','BOD-BOG'),('Bucaramanga','BOD-BOG'),('Cali','BOD-BOG')) AS b(ciudad, bodega_id) ON b.ciudad = p.ciudad
         WHERE p.estado = 'Pendiente de despacho' GROUP BY 1, 2)
SELECT i.sku, pr.nombre, pr.linea, pr.clase_abc, i.bodega_id, i.existencia_final AS existencia,
       round(dem.demanda_prom_30d, 1) AS demanda_prom_30d,
       round(i.existencia_final / nullif(dem.demanda_prom_30d, 0), 1) AS cobertura_dias,
       coalesce(pend.unidades_pendientes, 0) AS unidades_pendientes
FROM inventario_diario i JOIN productos pr USING (sku) JOIN dem USING (sku, bodega_id)
LEFT JOIN pend USING (sku, bodega_id) WHERE i.fecha = fecha_corte();

-- Líneas con descuento por encima del tope de la política y sin aprobación especial
CREATE OR REPLACE VIEW v_descuentos_fuera_politica AS
SELECT v.*, t.tope_descuento_pct,
       round(v.cantidad * v.precio_unitario * (v.descuento_pct - t.tope_descuento_pct) / 100, 0) AS descuento_en_exceso
FROM v_ventas v JOIN ref_topes_descuento t USING (segmento)
WHERE v.descuento_pct > t.tope_descuento_pct AND v.aprobacion_especial = 'N';

-- Actividad de compra por cliente (frecuencia habitual vs. días sin comprar)
CREATE OR REPLACE VIEW v_actividad_cliente AS
WITH p AS (SELECT cliente_id, fecha, lag(fecha) OVER (PARTITION BY cliente_id ORDER BY fecha) AS anterior
           FROM pedidos WHERE estado <> 'Cancelado' AND fecha <= fecha_corte())
SELECT cliente_id, count(*) AS pedidos, max(fecha) AS ultima_compra,
       round(avg(fecha - anterior), 1) AS intervalo_prom_dias, fecha_corte() - max(fecha) AS dias_sin_comprar,
       round((fecha_corte() - max(fecha)) / nullif(avg(fecha - anterior), 0), 1) AS veces_intervalo_habitual
FROM p GROUP BY cliente_id;
