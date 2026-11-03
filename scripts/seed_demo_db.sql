-- ==============================================================================
-- Demo Database Seeder for DBOps Testing
-- ==============================================================================
CREATE TABLE IF NOT EXISTS customers (
    id SERIAL PRIMARY KEY,
    name VARCHAR(100) NOT NULL,
    email VARCHAR(100) UNIQUE NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS orders (
    id SERIAL PRIMARY KEY,
    customer_id INT REFERENCES customers(id),
    amount NUMERIC(10, 2) NOT NULL,
    status VARCHAR(50) DEFAULT 'completed',
    order_date TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_orders_customer ON orders(customer_id);
CREATE INDEX IF NOT EXISTS idx_orders_date ON orders(order_date);

-- Insert sample rows if empty
INSERT INTO customers (name, email)
SELECT 'Customer ' || g, 'user_' || g || '@example.com'
FROM generate_series(1, 1000) g
ON CONFLICT (email) DO NOTHING;

INSERT INTO orders (customer_id, amount, status)
SELECT 
    (1 + (g % 1000)),
    (random() * 500)::numeric(10,2),
    CASE WHEN (g % 5 = 0) THEN 'pending' ELSE 'completed' END
FROM generate_series(1, 5000) g;

