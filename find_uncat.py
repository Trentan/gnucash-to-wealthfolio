from gnucash_to_wealthfolio.config import get_default_wealthfolio_db_path

db_path = get_default_wealthfolio_db_path()
conn = sqlite3.connect(db_path)
c = conn.cursor()

# How does Wealthfolio calculate "Where it went" / spending?
# Let's inspect the activities that might be considered uncategorized.
# 1. Activities with NO entry in activity_taxonomy_assignments where taxonomy_id = 'spending_categories'
c.execute("""
    SELECT a.id, acc.name, a.activity_type, a.amount, a.activity_date, a.notes, a.source_system
    FROM activities a
    JOIN accounts acc ON a.account_id = acc.id
    WHERE a.activity_type IN ('WITHDRAWAL', 'TRANSFER_OUT')
      AND NOT EXISTS (
          SELECT 1 FROM activity_taxonomy_assignments ata
          WHERE ata.activity_id = a.id AND ata.taxonomy_id = 'spending_categories'
      )
    ORDER BY CAST(a.amount AS NUMERIC) DESC
    LIMIT 30
""")
rows = c.fetchall()
print(f"Activities with no spending_categories assignment: {len(rows)}")
for r in rows[:20]:
    print(r[:6])

# Let's check the sum of amounts of these:
c.execute("""
    SELECT count(*), sum(CAST(a.amount AS NUMERIC))
    FROM activities a
    JOIN accounts acc ON a.account_id = acc.id
    WHERE a.activity_type IN ('WITHDRAWAL', 'TRANSFER_OUT')
      AND NOT EXISTS (
          SELECT 1 FROM activity_taxonomy_assignments ata
          WHERE ata.activity_id = a.id AND ata.taxonomy_id = 'spending_categories'
      )
""")
print("\nTotal sum of withdrawals/transfer_out without spending_categories:", c.fetchall())

# What about grouped by account and activity_type?
c.execute("""
    SELECT acc.name, a.activity_type, count(*), sum(CAST(a.amount AS NUMERIC))
    FROM activities a
    JOIN accounts acc ON a.account_id = acc.id
    WHERE a.activity_type IN ('WITHDRAWAL', 'TRANSFER_OUT')
      AND NOT EXISTS (
          SELECT 1 FROM activity_taxonomy_assignments ata
          WHERE ata.activity_id = a.id AND ata.taxonomy_id = 'spending_categories'
      )
    GROUP BY acc.name, a.activity_type
""")
for r in c.fetchall():
    print(" ", r)
