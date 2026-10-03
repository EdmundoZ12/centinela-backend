-- Centinela · esquema PostgreSQL del dataset (Distribuidora Andina S.A.S., empresa ficticia)
CREATE SCHEMA IF NOT EXISTS centinela;
SET search_path TO centinela;

CREATE TABLE vendedores (vendedor_id varchar(4) PRIMARY KEY, nombre text NOT NULL, region text NOT NULL);
CREATE TABLE clientes (
  cliente_id varchar(6) PRIMARY KEY, nombre text NOT NULL, segmento text NOT NULL, ciudad text NOT NULL,
  region text NOT NULL, vendedor_id varchar(4) REFERENCES vendedores, plazo_dias int NOT NULL,
  cupo_credito bigint NOT NULL, fecha_alta date NOT NULL);
CREATE TABLE proveedores (proveedor_id varchar(4) PRIMARY KEY, nombre text NOT NULL, lead_time_dias int NOT NULL, pais text NOT NULL);
CREATE TABLE productos (
  sku varchar(6) PRIMARY KEY, nombre text NOT NULL, linea text NOT NULL,
  proveedor_id varchar(4) REFERENCES proveedores, clase_abc char(1) NOT NULL, unidad varchar(4) NOT NULL);
CREATE TABLE bodegas (bodega_id varchar(8) PRIMARY KEY, nombre text NOT NULL, ciudad text NOT NULL);
CREATE TABLE lista_precios (sku varchar(6) REFERENCES productos, fecha_vigencia date NOT NULL, precio_lista numeric(14,2) NOT NULL,
  PRIMARY KEY (sku, fecha_vigencia));
CREATE TABLE costos_proveedor (sku varchar(6) REFERENCES productos, proveedor_id varchar(4) REFERENCES proveedores,
  fecha_vigencia date NOT NULL, costo_unitario numeric(14,2) NOT NULL, PRIMARY KEY (sku, fecha_vigencia));
CREATE TABLE ordenes_compra (
  oc_id varchar(10) PRIMARY KEY, proveedor_id varchar(4) REFERENCES proveedores, sku varchar(6) REFERENCES productos,
  bodega_id varchar(8) REFERENCES bodegas, fecha_oc date NOT NULL, fecha_esperada date NOT NULL, fecha_recibida date,
  cantidad int NOT NULL, costo_unitario numeric(14,2) NOT NULL, estado text NOT NULL);
CREATE TABLE pedidos (
  pedido_id varchar(10) PRIMARY KEY, fecha date NOT NULL, cliente_id varchar(6) REFERENCES clientes,
  vendedor_id varchar(4) REFERENCES vendedores, ciudad text NOT NULL, canal text NOT NULL, estado text NOT NULL);
CREATE TABLE pedidos_detalle (
  pedido_id varchar(10) REFERENCES pedidos, linea_n int NOT NULL, sku varchar(6) REFERENCES productos,
  cantidad int NOT NULL, precio_lista numeric(14,2) NOT NULL, precio_unitario numeric(14,2) NOT NULL,
  descuento_pct numeric(5,2) NOT NULL, aprobacion_especial char(1) NOT NULL, valor_neto numeric(16,2) NOT NULL,
  costo_unitario numeric(14,2) NOT NULL, PRIMARY KEY (pedido_id, linea_n));
CREATE TABLE facturas (
  factura_id varchar(10) PRIMARY KEY, pedido_id varchar(10) REFERENCES pedidos, cliente_id varchar(6) REFERENCES clientes,
  fecha_factura date NOT NULL, fecha_vencimiento date NOT NULL, valor_neto numeric(16,2) NOT NULL,
  iva numeric(16,2) NOT NULL, valor_total numeric(16,2) NOT NULL);
CREATE TABLE pagos (pago_id varchar(10) PRIMARY KEY, factura_id varchar(10) REFERENCES facturas, fecha_pago date NOT NULL,
  valor numeric(16,2) NOT NULL, medio_pago text NOT NULL);
CREATE TABLE inventario_diario (
  fecha date NOT NULL, bodega_id varchar(8) REFERENCES bodegas, sku varchar(6) REFERENCES productos,
  existencia_inicial int NOT NULL, entradas int NOT NULL, salidas int NOT NULL, existencia_final int NOT NULL,
  PRIMARY KEY (fecha, bodega_id, sku));
CREATE TABLE ref_topes_descuento (segmento text PRIMARY KEY, tope_descuento_pct numeric(5,2) NOT NULL);
CREATE TABLE ref_margen_minimo_linea (linea text PRIMARY KEY, margen_minimo_pct numeric(5,2) NOT NULL);

CREATE INDEX ON pedidos (fecha); CREATE INDEX ON pedidos (cliente_id); CREATE INDEX ON pedidos_detalle (sku);
CREATE INDEX ON facturas (cliente_id); CREATE INDEX ON pagos (factura_id); CREATE INDEX ON inventario_diario (sku, bodega_id);
