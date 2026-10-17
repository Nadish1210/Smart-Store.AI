-- =====================================================================
-- SmartStock AI v2 - database_schema.sql
-- Run ONCE in the Supabase SQL Editor (Dashboard > SQL Editor > New query).
--
-- Security model
--   * Browsers/shop users can only SELECT (through RLS) rows of their own shop.
--   * ALL writes go through SECURITY DEFINER functions that derive the shop
--     from auth.uid(). A shop_id sent by the browser is never trusted.
--   * Suspended shops: current_shop_id() returns NULL, so every policy and
--     function denies them (also for direct REST requests).
--   * Super admin = a row in app_admins. That table has no API access; rows
--     can only be added from the SQL Editor (see section 8).
--   * Maximum 10 shops is enforced by a trigger on shops.
-- =====================================================================

-- ---------------------------------------------------------------------
-- 1. Tables
-- ---------------------------------------------------------------------
create table if not exists public.app_admins (
  user_id    uuid primary key references auth.users (id) on delete cascade,
  created_at timestamptz not null default now()
);

create table if not exists public.shops (
  id            uuid primary key default gen_random_uuid(),
  shop_number   integer generated always as identity unique,
  owner_user_id uuid not null unique references auth.users (id) on delete cascade,
  shop_name     text not null check (char_length(btrim(shop_name)) between 1 and 120),
  owner_name    text not null check (char_length(btrim(owner_name)) between 1 and 120),
  email         text not null check (email ~* '^[^@\s]+@[^@\s]+\.[^@\s]+$'),
  phone         text not null default '' check (char_length(phone) <= 40),
  status        text not null default 'active' check (status in ('active', 'suspended')),
  created_at    timestamptz not null default now(),
  updated_at    timestamptz not null default now()
);
create unique index if not exists shops_email_key on public.shops (lower(email));

create table if not exists public.shop_counters (
  shop_id uuid not null references public.shops (id) on delete cascade,
  kind    text not null check (kind in ('product', 'sale', 'movement', 'supplier')),
  value   bigint not null default 0,
  primary key (shop_id, kind)
);

create table if not exists public.suppliers (
  id             uuid primary key default gen_random_uuid(),
  shop_id        uuid not null references public.shops (id) on delete cascade,
  supplier_id    text not null,
  name           text not null check (char_length(btrim(name)) between 1 and 120),
  contact_person text not null default '' check (char_length(contact_person) <= 120),
  phone          text not null default '' check (char_length(phone) <= 40),
  email          text not null default '' check (char_length(email) <= 120),
  address        text not null default '' check (char_length(address) <= 250),
  notes          text not null default '' check (char_length(notes) <= 1000),
  created_at     timestamptz not null default now(),
  unique (shop_id, supplier_id)
);
create unique index if not exists suppliers_shop_name_key on public.suppliers (shop_id, lower(name));

create table if not exists public.products (
  id             uuid primary key default gen_random_uuid(),
  shop_id        uuid not null references public.shops (id) on delete cascade,
  product_id     text not null,
  barcode        text not null check (barcode ~ '^[!-~]{1,40}$'),
  name           text not null check (char_length(btrim(name)) between 1 and 120),
  category       text not null check (char_length(btrim(category)) between 1 and 60),
  purchase_price numeric(12, 2) not null check (purchase_price >= 0),
  selling_price  numeric(12, 2) not null check (selling_price >= 0),
  quantity       integer not null check (quantity >= 0 and quantity <= 1000000000),
  reorder_level  integer not null default 0 check (reorder_level >= 0),
  supplier       text not null default '' check (char_length(supplier) <= 120),
  expiry_date    date,
  created_at     timestamptz not null default now(),
  unique (shop_id, product_id),
  unique (shop_id, barcode)          -- barcode is unique per shop only
);
create index if not exists products_shop_category_idx on public.products (shop_id, category);

create table if not exists public.sales (
  id           uuid primary key default gen_random_uuid(),
  seq          bigint generated always as identity,
  shop_id      uuid not null references public.shops (id) on delete cascade,
  sale_id      text not null,
  product_ref  uuid references public.products (id) on delete set null,
  product_id   text not null,
  barcode      text not null default '',
  product_name text not null,
  quantity     integer not null check (quantity > 0),
  unit_price   numeric(12, 2) not null check (unit_price >= 0),
  unit_cost    numeric(12, 2) not null check (unit_cost >= 0),
  total        numeric(14, 2) not null check (total >= 0),
  profit       numeric(14, 2) not null,
  request_id   uuid,
  created_at   timestamptz not null default now(),
  unique (shop_id, sale_id),
  unique (shop_id, request_id)       -- duplicate-processing guard
);
create index if not exists sales_shop_created_idx on public.sales (shop_id, created_at);
create index if not exists sales_shop_product_idx on public.sales (shop_id, product_ref);

create table if not exists public.stock_movements (
  id              uuid primary key default gen_random_uuid(),
  seq             bigint generated always as identity,
  shop_id         uuid not null references public.shops (id) on delete cascade,
  movement_id     text not null,
  product_ref     uuid references public.products (id) on delete set null,
  product_id      text not null,
  barcode         text not null default '',
  product_name    text not null,
  type            text not null check (type in ('INITIAL', 'IN', 'OUT', 'ADJUST')),
  quantity_change integer not null,
  quantity_after  integer not null check (quantity_after >= 0),
  note            text not null default '' check (char_length(note) <= 500),
  request_id      uuid,
  created_at      timestamptz not null default now(),
  unique (shop_id, movement_id),
  unique (shop_id, request_id)
);
create index if not exists movements_shop_created_idx on public.stock_movements (shop_id, created_at);
create index if not exists movements_shop_product_idx on public.stock_movements (shop_id, product_ref);

-- ---------------------------------------------------------------------
-- 2. Identity helpers (used by policies and functions)
-- ---------------------------------------------------------------------
create or replace function public.is_super_admin()
returns boolean language sql stable security definer set search_path = public as $$
  select exists (select 1 from public.app_admins where user_id = auth.uid());
$$;

-- Shop of the logged-in user, or NULL when none / suspended.
create or replace function public.current_shop_id()
returns uuid language sql stable security definer set search_path = public as $$
  select id from public.shops where owner_user_id = auth.uid() and status = 'active';
$$;

-- ---------------------------------------------------------------------
-- 3. Row Level Security (read-only for shop users; writes via functions)
-- ---------------------------------------------------------------------
alter table public.app_admins      enable row level security;
alter table public.shops           enable row level security;
alter table public.shop_counters   enable row level security;
alter table public.suppliers       enable row level security;
alter table public.products        enable row level security;
alter table public.sales           enable row level security;
alter table public.stock_movements enable row level security;

revoke all on all tables in schema public from anon, authenticated;
grant select on public.shops, public.suppliers, public.products,
                public.sales, public.stock_movements to authenticated;

drop policy if exists shops_select on public.shops;
create policy shops_select on public.shops for select to authenticated
  using (owner_user_id = auth.uid() or public.is_super_admin());

drop policy if exists suppliers_select on public.suppliers;
create policy suppliers_select on public.suppliers for select to authenticated
  using (shop_id = public.current_shop_id());

drop policy if exists products_select on public.products;
create policy products_select on public.products for select to authenticated
  using (shop_id = public.current_shop_id());

drop policy if exists sales_select on public.sales;
create policy sales_select on public.sales for select to authenticated
  using (shop_id = public.current_shop_id());

drop policy if exists movements_select on public.stock_movements;
create policy movements_select on public.stock_movements for select to authenticated
  using (shop_id = public.current_shop_id());
-- app_admins and shop_counters: RLS on, NO policies => no API access at all.

-- ---------------------------------------------------------------------
-- 4. Shop limit (max 10) - enforced by the database
-- ---------------------------------------------------------------------
create or replace function public.enforce_shop_limit()
returns trigger language plpgsql as $$
begin
  perform pg_advisory_xact_lock(hashtext('smartstock_shop_limit'));
  if (select count(*) from public.shops) >= 10 then
    raise exception 'Shop limit reached: a maximum of 10 shops is allowed.' using errcode = 'P0001';
  end if;
  return new;
end $$;

drop trigger if exists shops_limit_trg on public.shops;
create trigger shops_limit_trg before insert on public.shops
  for each row execute function public.enforce_shop_limit();

-- ---------------------------------------------------------------------
-- 5. Internal helpers (not callable from the API)
-- ---------------------------------------------------------------------
create or replace function public.next_code(p_shop uuid, p_kind text, p_prefix text)
returns text language plpgsql security definer set search_path = public as $$
declare v bigint;
begin
  insert into public.shop_counters (shop_id, kind, value) values (p_shop, p_kind, 1)
  on conflict (shop_id, kind) do update set value = public.shop_counters.value + 1
  returning value into v;
  return p_prefix || lpad(v::text, 4, '0');
end $$;

create or replace function public.log_movement(
  p_shop uuid, p_product uuid, p_type text, p_change integer, p_note text, p_request uuid)
returns public.stock_movements language plpgsql security definer set search_path = public as $$
declare p public.products; m public.stock_movements;
begin
  select * into p from public.products where id = p_product;
  insert into public.stock_movements
    (shop_id, movement_id, product_ref, product_id, barcode, product_name,
     type, quantity_change, quantity_after, note, request_id)
  values
    (p_shop, public.next_code(p_shop, 'movement', 'M'), p.id, p.product_id, p.barcode, p.name,
     p_type, p_change, p.quantity, coalesce(p_note, ''), p_request)
  returning * into m;
  return m;
end $$;

create or replace function public.fmt_ts(t timestamptz)
returns text language sql immutable as $$
  select to_char(t at time zone 'UTC', 'YYYY-MM-DD HH24:MI:SS');
$$;

create or replace function public.build_shop_backup(p_shop uuid)
returns jsonb language sql stable security definer set search_path = public as $$
  select jsonb_build_object(
    'app', 'SmartStock AI', 'version', 2, 'shop_id', p_shop,
    'exported_at', public.fmt_ts(now()),
    'products', coalesce((select jsonb_agg(jsonb_build_object(
        'product_id', product_id, 'barcode', barcode, 'name', name, 'category', category,
        'purchase_price', purchase_price, 'selling_price', selling_price, 'quantity', quantity,
        'reorder_level', reorder_level, 'supplier', supplier,
        'expiry_date', coalesce(expiry_date::text, ''), 'created_at', public.fmt_ts(created_at))
        order by created_at, length(product_id), product_id) from public.products where shop_id = p_shop), '[]'::jsonb),
    'sales', coalesce((select jsonb_agg(jsonb_build_object(
        'sale_id', sale_id, 'timestamp', public.fmt_ts(created_at), 'product_id', product_id,
        'barcode', barcode, 'product_name', product_name, 'quantity', quantity,
        'unit_price', unit_price, 'unit_cost', unit_cost, 'total', total, 'profit', profit)
        order by seq) from public.sales where shop_id = p_shop), '[]'::jsonb),
    'movements', coalesce((select jsonb_agg(jsonb_build_object(
        'movement_id', movement_id, 'timestamp', public.fmt_ts(created_at), 'product_id', product_id,
        'barcode', barcode, 'product_name', product_name, 'type', type,
        'quantity_change', quantity_change, 'quantity_after', quantity_after, 'note', note)
        order by seq) from public.stock_movements where shop_id = p_shop), '[]'::jsonb),
    'suppliers', coalesce((select jsonb_agg(jsonb_build_object(
        'supplier_id', supplier_id, 'name', name, 'contact_person', contact_person,
        'phone', phone, 'email', email, 'address', address, 'notes', notes)
        order by created_at, length(supplier_id), supplier_id) from public.suppliers where shop_id = p_shop), '[]'::jsonb)
  );
$$;

-- ---------------------------------------------------------------------
-- 6. Functions for SHOP USERS (shop is derived from auth.uid())
-- ---------------------------------------------------------------------
create or replace function public.my_profile()
returns jsonb language plpgsql stable security definer set search_path = public as $$
declare s public.shops;
begin
  if auth.uid() is null then return jsonb_build_object('role', null); end if;
  if public.is_super_admin() then
    return jsonb_build_object('role', 'super_admin', 'user_id', auth.uid());
  end if;
  select * into s from public.shops where owner_user_id = auth.uid();
  if not found then return jsonb_build_object('role', null); end if;
  return jsonb_build_object('role', 'shop', 'user_id', auth.uid(), 'shop_id', s.id,
    'shop_name', s.shop_name, 'owner_name', s.owner_name, 'email', s.email,
    'phone', s.phone, 'status', s.status);
end $$;

create or replace function public.add_product(
  p_name text, p_category text, p_purchase_price numeric, p_selling_price numeric,
  p_quantity integer, p_reorder_level integer, p_barcode text default '',
  p_supplier text default '', p_expiry_date date default null)
returns jsonb language plpgsql security definer set search_path = public as $$
declare v_shop uuid := public.current_shop_id(); v_code text := btrim(coalesce(p_barcode, ''));
        v_row public.products; v_other text;
begin
  if v_shop is null then raise exception 'Your account is not active.' using errcode = '42501'; end if;
  if v_code = '' then
    loop
      v_code := lpad((floor(random() * 1e12))::bigint::text, 12, '0');
      exit when not exists (select 1 from public.products where shop_id = v_shop and barcode = v_code);
    end loop;
  elsif v_code !~ '^[!-~]{1,40}$' then
    raise exception 'Barcode must be 1-40 printable ASCII characters with no spaces.';
  end if;
  select name into v_other from public.products where shop_id = v_shop and barcode = v_code;
  if found then
    raise exception 'Barcode % already belongs to ''%''.', v_code, v_other using errcode = '23505';
  end if;
  insert into public.products (shop_id, product_id, barcode, name, category, purchase_price,
      selling_price, quantity, reorder_level, supplier, expiry_date)
  values (v_shop, public.next_code(v_shop, 'product', 'P'), v_code, btrim(p_name), btrim(p_category),
      p_purchase_price, p_selling_price, p_quantity, p_reorder_level,
      coalesce(btrim(p_supplier), ''), p_expiry_date)
  returning * into v_row;
  if v_row.quantity > 0 then
    perform public.log_movement(v_shop, v_row.id, 'INITIAL', v_row.quantity, 'Opening stock', null);
  end if;
  return to_jsonb(v_row);
end $$;

create or replace function public.edit_product(
  p_product_id text, p_name text, p_category text, p_purchase_price numeric,
  p_selling_price numeric, p_quantity integer, p_reorder_level integer, p_barcode text,
  p_supplier text default '', p_expiry_date date default null)
returns jsonb language plpgsql security definer set search_path = public as $$
declare v_shop uuid := public.current_shop_id(); v_old public.products; v_row public.products;
        v_code text := btrim(coalesce(p_barcode, '')); v_other text;
begin
  if v_shop is null then raise exception 'Your account is not active.' using errcode = '42501'; end if;
  select * into v_old from public.products where shop_id = v_shop and product_id = p_product_id for update;
  if not found then raise exception 'Product not found.'; end if;
  if v_code !~ '^[!-~]{1,40}$' then
    raise exception 'Barcode must be 1-40 printable ASCII characters with no spaces.';
  end if;
  select name into v_other from public.products
    where shop_id = v_shop and barcode = v_code and id <> v_old.id;
  if found then
    raise exception 'Barcode % already belongs to ''%''.', v_code, v_other using errcode = '23505';
  end if;
  update public.products set name = btrim(p_name), category = btrim(p_category),
      purchase_price = p_purchase_price, selling_price = p_selling_price, quantity = p_quantity,
      reorder_level = p_reorder_level, barcode = v_code, supplier = coalesce(btrim(p_supplier), ''),
      expiry_date = p_expiry_date
   where id = v_old.id returning * into v_row;
  if v_row.quantity <> v_old.quantity then
    perform public.log_movement(v_shop, v_row.id, 'ADJUST', v_row.quantity - v_old.quantity,
                                'Manual adjustment from product edit', null);
  end if;
  return to_jsonb(v_row);
end $$;

create or replace function public.delete_product(p_product_id text)
returns jsonb language plpgsql security definer set search_path = public as $$
declare v_shop uuid := public.current_shop_id(); v_name text;
begin
  if v_shop is null then raise exception 'Your account is not active.' using errcode = '42501'; end if;
  delete from public.products where shop_id = v_shop and product_id = p_product_id returning name into v_name;
  if not found then raise exception 'Product not found.'; end if;
  return jsonb_build_object('name', v_name);
end $$;

create or replace function public.stock_in(
  p_product_id text, p_quantity integer, p_note text default '', p_request_id uuid default null)
returns jsonb language plpgsql security definer set search_path = public as $$
declare v_shop uuid := public.current_shop_id(); v_p public.products; v_m public.stock_movements;
begin
  if v_shop is null then raise exception 'Your account is not active.' using errcode = '42501'; end if;
  select * into v_p from public.products where shop_id = v_shop and product_id = p_product_id for update;
  if not found then raise exception 'Product not found. Add it first on the Add Product page.'; end if;
  if p_request_id is not null then
    select * into v_m from public.stock_movements where shop_id = v_shop and request_id = p_request_id;
    if found then
      return jsonb_build_object('product', to_jsonb(v_p), 'movement', to_jsonb(v_m), 'duplicate', true);
    end if;
  end if;
  if p_quantity is null or p_quantity < 1 then
    raise exception 'Received quantity must be at least 1.';
  end if;
  update public.products set quantity = quantity + p_quantity where id = v_p.id returning * into v_p;
  v_m := public.log_movement(v_shop, v_p.id, 'IN', p_quantity,
           coalesce(nullif(btrim(p_note), ''), 'Stock received'), p_request_id);
  return jsonb_build_object('product', to_jsonb(v_p), 'movement', to_jsonb(v_m), 'duplicate', false);
end $$;

create or replace function public.record_sale(
  p_product_id text, p_quantity integer, p_request_id uuid default null)
returns jsonb language plpgsql security definer set search_path = public as $$
declare v_shop uuid := public.current_shop_id(); v_p public.products; v_s public.sales;
        v_total numeric; v_cost numeric;
begin
  if v_shop is null then raise exception 'Your account is not active.' using errcode = '42501'; end if;
  -- Row lock serialises simultaneous sales of the same product.
  select * into v_p from public.products where shop_id = v_shop and product_id = p_product_id for update;
  if not found then raise exception 'Product not found.'; end if;
  if p_request_id is not null then
    select * into v_s from public.sales where shop_id = v_shop and request_id = p_request_id;
    if found then
      return jsonb_build_object('product', to_jsonb(v_p), 'sale', to_jsonb(v_s), 'duplicate', true);
    end if;
  end if;
  if p_quantity is null or p_quantity < 1 then raise exception 'Quantity sold must be at least 1.'; end if;
  if p_quantity > v_p.quantity then
    raise exception 'Not enough stock: only % unit(s) of ''%'' available, cannot sell %.',
      v_p.quantity, v_p.name, p_quantity using errcode = '23514';
  end if;
  v_total := round(p_quantity * v_p.selling_price, 2);
  v_cost  := round(p_quantity * v_p.purchase_price, 2);
  update public.products set quantity = quantity - p_quantity where id = v_p.id returning * into v_p;
  insert into public.sales (shop_id, sale_id, product_ref, product_id, barcode, product_name,
      quantity, unit_price, unit_cost, total, profit, request_id)
  values (v_shop, public.next_code(v_shop, 'sale', 'S'), v_p.id, v_p.product_id, v_p.barcode, v_p.name,
      p_quantity, v_p.selling_price, v_p.purchase_price, v_total, v_total - v_cost, p_request_id)
  returning * into v_s;
  perform public.log_movement(v_shop, v_p.id, 'OUT', -p_quantity, 'Sale ' || v_s.sale_id, null);
  return jsonb_build_object('product', to_jsonb(v_p), 'sale', to_jsonb(v_s), 'duplicate', false);
end $$;

create or replace function public.add_supplier(
  p_name text, p_contact_person text default '', p_phone text default '',
  p_email text default '', p_address text default '', p_notes text default '')
returns jsonb language plpgsql security definer set search_path = public as $$
declare v_shop uuid := public.current_shop_id(); v_row public.suppliers;
begin
  if v_shop is null then raise exception 'Your account is not active.' using errcode = '42501'; end if;
  if exists (select 1 from public.suppliers where shop_id = v_shop and lower(name) = lower(btrim(p_name))) then
    raise exception 'A supplier named ''%'' already exists.', btrim(p_name) using errcode = '23505';
  end if;
  insert into public.suppliers (shop_id, supplier_id, name, contact_person, phone, email, address, notes)
  values (v_shop, public.next_code(v_shop, 'supplier', 'SUP'), btrim(p_name), btrim(coalesce(p_contact_person, '')),
          btrim(coalesce(p_phone, '')), btrim(coalesce(p_email, '')), btrim(coalesce(p_address, '')),
          btrim(coalesce(p_notes, '')))
  returning * into v_row;
  return to_jsonb(v_row);
end $$;

create or replace function public.edit_supplier(
  p_supplier_id text, p_name text, p_contact_person text default '', p_phone text default '',
  p_email text default '', p_address text default '', p_notes text default '')
returns jsonb language plpgsql security definer set search_path = public as $$
declare v_shop uuid := public.current_shop_id(); v_old public.suppliers; v_row public.suppliers;
begin
  if v_shop is null then raise exception 'Your account is not active.' using errcode = '42501'; end if;
  select * into v_old from public.suppliers where shop_id = v_shop and supplier_id = p_supplier_id for update;
  if not found then raise exception 'Supplier not found.'; end if;
  if exists (select 1 from public.suppliers where shop_id = v_shop and id <> v_old.id
             and lower(name) = lower(btrim(p_name))) then
    raise exception 'A supplier named ''%'' already exists.', btrim(p_name) using errcode = '23505';
  end if;
  update public.suppliers set name = btrim(p_name), contact_person = btrim(coalesce(p_contact_person, '')),
      phone = btrim(coalesce(p_phone, '')), email = btrim(coalesce(p_email, '')),
      address = btrim(coalesce(p_address, '')), notes = btrim(coalesce(p_notes, ''))
   where id = v_old.id returning * into v_row;
  if v_old.name <> v_row.name then
    update public.products set supplier = v_row.name where shop_id = v_shop and supplier = v_old.name;
  end if;
  return to_jsonb(v_row);
end $$;

create or replace function public.delete_supplier(p_supplier_id text)
returns jsonb language plpgsql security definer set search_path = public as $$
declare v_shop uuid := public.current_shop_id(); v_name text; v_cleared integer;
begin
  if v_shop is null then raise exception 'Your account is not active.' using errcode = '42501'; end if;
  delete from public.suppliers where shop_id = v_shop and supplier_id = p_supplier_id returning name into v_name;
  if not found then raise exception 'Supplier not found.'; end if;
  update public.products set supplier = '' where shop_id = v_shop and supplier = v_name;
  get diagnostics v_cleared = row_count;
  return jsonb_build_object('name', v_name, 'cleared', v_cleared);
end $$;

create or replace function public.my_backup()
returns jsonb language plpgsql stable security definer set search_path = public as $$
declare v_shop uuid := public.current_shop_id();
begin
  if v_shop is null then raise exception 'Your account is not active.' using errcode = '42501'; end if;
  return public.build_shop_backup(v_shop);
end $$;

-- Replaces ALL data of the caller's shop with a backup, in ONE transaction.
-- A shop_id inside the file is never used; a mismatching one is rejected.
create or replace function public.restore_backup(p_data jsonb)
returns jsonb language plpgsql security definer set search_path = public as $$
declare v_shop uuid := public.current_shop_id();
begin
  if v_shop is null then raise exception 'Your account is not active.' using errcode = '42501'; end if;
  if jsonb_typeof(p_data) <> 'object' then raise exception 'Backup must be a JSON object.'; end if;
  if coalesce(p_data->>'shop_id', '') <> '' and p_data->>'shop_id' <> v_shop::text then
    raise exception 'This backup belongs to a different shop and cannot be restored here.' using errcode = '42501';
  end if;
  perform pg_advisory_xact_lock(hashtext('restore:' || v_shop::text));

  delete from public.sales where shop_id = v_shop;
  delete from public.stock_movements where shop_id = v_shop;
  delete from public.products where shop_id = v_shop;
  delete from public.suppliers where shop_id = v_shop;
  delete from public.shop_counters where shop_id = v_shop;

  insert into public.suppliers (shop_id, supplier_id, name, contact_person, phone, email, address, notes)
  select v_shop, e->>'supplier_id', e->>'name', coalesce(e->>'contact_person', ''), coalesce(e->>'phone', ''),
         coalesce(e->>'email', ''), coalesce(e->>'address', ''), coalesce(e->>'notes', '')
    from jsonb_array_elements(coalesce(p_data->'suppliers', '[]'::jsonb)) with ordinality as t(e, ord)
   order by ord;

  insert into public.products (shop_id, product_id, barcode, name, category, purchase_price, selling_price,
      quantity, reorder_level, supplier, expiry_date, created_at)
  select v_shop, e->>'product_id', e->>'barcode', e->>'name', e->>'category',
         (e->>'purchase_price')::numeric, (e->>'selling_price')::numeric, (e->>'quantity')::integer,
         (e->>'reorder_level')::integer, coalesce(e->>'supplier', ''), nullif(e->>'expiry_date', '')::date,
         ((e->>'created_at')::timestamp at time zone 'UTC')
    from jsonb_array_elements(coalesce(p_data->'products', '[]'::jsonb)) with ordinality as t(e, ord)
   order by ord;

  insert into public.sales (shop_id, sale_id, product_ref, product_id, barcode, product_name, quantity,
      unit_price, unit_cost, total, profit, created_at)
  select v_shop, e->>'sale_id',
         (select p.id from public.products p where p.shop_id = v_shop and p.product_id = e->>'product_id'),
         e->>'product_id', coalesce(e->>'barcode', ''), e->>'product_name', (e->>'quantity')::integer,
         (e->>'unit_price')::numeric, (e->>'unit_cost')::numeric, (e->>'total')::numeric,
         (e->>'profit')::numeric, ((e->>'timestamp')::timestamp at time zone 'UTC')
    from jsonb_array_elements(coalesce(p_data->'sales', '[]'::jsonb)) with ordinality as t(e, ord)
   order by ord;

  insert into public.stock_movements (shop_id, movement_id, product_ref, product_id, barcode, product_name,
      type, quantity_change, quantity_after, note, created_at)
  select v_shop, e->>'movement_id',
         (select p.id from public.products p where p.shop_id = v_shop and p.product_id = e->>'product_id'),
         e->>'product_id', coalesce(e->>'barcode', ''), e->>'product_name', e->>'type',
         (e->>'quantity_change')::integer, (e->>'quantity_after')::integer, coalesce(e->>'note', ''),
         ((e->>'timestamp')::timestamp at time zone 'UTC')
    from jsonb_array_elements(coalesce(p_data->'movements', '[]'::jsonb)) with ordinality as t(e, ord)
   order by ord;

  insert into public.shop_counters (shop_id, kind, value)
  select v_shop, k.kind, k.value from (
    select 'product' as kind, coalesce(max(substring(product_id from '(\d+)$')::bigint), 0) as value
      from public.products where shop_id = v_shop
    union all select 'sale', coalesce(max(substring(sale_id from '(\d+)$')::bigint), 0)
      from public.sales where shop_id = v_shop
    union all select 'movement', coalesce(max(substring(movement_id from '(\d+)$')::bigint), 0)
      from public.stock_movements where shop_id = v_shop
    union all select 'supplier', coalesce(max(substring(supplier_id from '(\d+)$')::bigint), 0)
      from public.suppliers where shop_id = v_shop
  ) k;

  return jsonb_build_object(
    'products', (select count(*) from public.products where shop_id = v_shop),
    'sales', (select count(*) from public.sales where shop_id = v_shop),
    'movements', (select count(*) from public.stock_movements where shop_id = v_shop),
    'suppliers', (select count(*) from public.suppliers where shop_id = v_shop));
end $$;

-- ---------------------------------------------------------------------
-- 7. Functions for the SUPER ADMIN (each re-checks the role in the database)
-- ---------------------------------------------------------------------
create or replace function public.admin_overview()
returns jsonb language plpgsql stable security definer set search_path = public as $$
begin
  if not public.is_super_admin() then raise exception 'Administrator access required.' using errcode = '42501'; end if;
  return jsonb_build_object(
    'total', (select count(*) from public.shops),
    'active', (select count(*) from public.shops where status = 'active'),
    'suspended', (select count(*) from public.shops where status = 'suspended'),
    'max_shops', 10,
    'slots', greatest(0, 10 - (select count(*) from public.shops)));
end $$;

create or replace function public.admin_can_create_shop(p_email text)
returns void language plpgsql stable security definer set search_path = public as $$
begin
  if not public.is_super_admin() then raise exception 'Administrator access required.' using errcode = '42501'; end if;
  if (select count(*) from public.shops) >= 10 then
    raise exception 'Shop limit reached: a maximum of 10 shops is allowed.' using errcode = 'P0001';
  end if;
  if exists (select 1 from public.shops where lower(email) = lower(btrim(p_email))) then
    raise exception 'A shop with this email address already exists.' using errcode = '23505';
  end if;
end $$;

create or replace function public.admin_email_available(p_email text, p_exclude uuid default null)
returns boolean language plpgsql stable security definer set search_path = public as $$
begin
  if not public.is_super_admin() then raise exception 'Administrator access required.' using errcode = '42501'; end if;
  return not exists (select 1 from public.shops
                     where lower(email) = lower(btrim(p_email)) and (p_exclude is null or id <> p_exclude));
end $$;

create or replace function public.admin_register_shop(
  p_user_id uuid, p_shop_name text, p_owner_name text, p_email text,
  p_phone text default '', p_status text default 'active')
returns jsonb language plpgsql security definer set search_path = public as $$
declare v_row public.shops;
begin
  if not public.is_super_admin() then raise exception 'Administrator access required.' using errcode = '42501'; end if;
  if exists (select 1 from public.app_admins where user_id = p_user_id) then
    raise exception 'An administrator account cannot be used as a shop.';
  end if;
  if exists (select 1 from public.shops where lower(email) = lower(btrim(p_email))) then
    raise exception 'A shop with this email address already exists.' using errcode = '23505';
  end if;
  insert into public.shops (owner_user_id, shop_name, owner_name, email, phone, status)
  values (p_user_id, btrim(p_shop_name), btrim(p_owner_name), btrim(p_email), btrim(coalesce(p_phone, '')), p_status)
  returning * into v_row;
  return to_jsonb(v_row);
end $$;

create or replace function public.admin_update_shop(
  p_shop_id uuid, p_shop_name text, p_owner_name text, p_email text, p_phone text default '')
returns jsonb language plpgsql security definer set search_path = public as $$
declare v_row public.shops;
begin
  if not public.is_super_admin() then raise exception 'Administrator access required.' using errcode = '42501'; end if;
  if exists (select 1 from public.shops where lower(email) = lower(btrim(p_email)) and id <> p_shop_id) then
    raise exception 'A shop with this email address already exists.' using errcode = '23505';
  end if;
  update public.shops set shop_name = btrim(p_shop_name), owner_name = btrim(p_owner_name),
      email = btrim(p_email), phone = btrim(coalesce(p_phone, '')), updated_at = now()
   where id = p_shop_id returning * into v_row;
  if not found then raise exception 'Shop not found.'; end if;
  return to_jsonb(v_row);
end $$;

create or replace function public.admin_set_shop_status(p_shop_id uuid, p_status text)
returns jsonb language plpgsql security definer set search_path = public as $$
declare v_row public.shops;
begin
  if not public.is_super_admin() then raise exception 'Administrator access required.' using errcode = '42501'; end if;
  if p_status not in ('active', 'suspended') then raise exception 'Invalid status.'; end if;
  update public.shops set status = p_status, updated_at = now() where id = p_shop_id returning * into v_row;
  if not found then raise exception 'Shop not found.'; end if;
  return to_jsonb(v_row);
end $$;

create or replace function public.admin_export_shop(p_shop_id uuid)
returns jsonb language plpgsql stable security definer set search_path = public as $$
begin
  if not public.is_super_admin() then raise exception 'Administrator access required.' using errcode = '42501'; end if;
  if not exists (select 1 from public.shops where id = p_shop_id) then raise exception 'Shop not found.'; end if;
  return public.build_shop_backup(p_shop_id);
end $$;

-- ---------------------------------------------------------------------
-- 8. Function permissions
-- ---------------------------------------------------------------------
do $$
declare r record;
begin
  for r in
    select format('%I.%I(%s)', n.nspname, p.proname, pg_get_function_identity_arguments(p.oid)) as sig, p.proname
      from pg_proc p join pg_namespace n on n.oid = p.pronamespace
     where n.nspname = 'public' and p.prokind = 'f'
  loop
    execute format('revoke all on function %s from public, anon, authenticated', r.sig);
    if r.proname in ('is_super_admin', 'current_shop_id', 'my_profile', 'add_product', 'edit_product',
        'delete_product', 'stock_in', 'record_sale', 'add_supplier', 'edit_supplier', 'delete_supplier',
        'my_backup', 'restore_backup', 'admin_overview', 'admin_can_create_shop', 'admin_email_available',
        'admin_register_shop', 'admin_update_shop', 'admin_set_shop_status', 'admin_export_shop') then
      execute format('grant execute on function %s to authenticated', r.sig);
    end if;
  end loop;
end $$;

-- ---------------------------------------------------------------------
-- 9. Create the FIRST SUPER ADMIN (run manually, once)
-- ---------------------------------------------------------------------
-- a) Dashboard > Authentication > Users > Add user > Create new user
--    (enter your admin email + a strong password, tick "Auto Confirm User").
-- b) Then run this statement with YOUR admin email:
--
--   insert into public.app_admins (user_id)
--   select id from auth.users where email = 'YOUR-ADMIN-EMAIL@example.com';
--
-- Nobody can run this through the API: app_admins has no policies and no grants.
