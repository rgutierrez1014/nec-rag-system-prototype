from psycopg2.extras import Json, execute_values
from pgvector.psycopg2 import register_vector


def fetch_embedded_npi_numbers(conn) -> set[str]:
    cur = conn.cursor()
    cur.execute("SELECT npi_number FROM practices WHERE embedding IS NOT NULL")
    result = {row[0] for row in cur.fetchall()}
    cur.close()
    return result


def upsert_practices(conn, practices: list[dict]) -> None:
    register_vector(conn)
    cur = conn.cursor()

    values = [
        (
            p["npi_number"], p["name"], p["description"], p["practice_type"],
            p["address_1"], p["address_city"], p["address_state"], p["address_zip"],
            p["neighborhood"],
            p["services"], p["specialties"], p["presence_types"],
            Json(p["professionals"]),
            p["embedding"], p["embedding_model"],
        )
        for p in practices
    ]

    execute_values(
        cur,
        """
        INSERT INTO practices (
            npi_number, name, description, practice_type,
            address_1, address_city, address_state, address_zip,
            neighborhood,
            services, specialties, presence_types, professionals,
            embedding, embedding_model,
            updated_at
        ) VALUES %s
        ON CONFLICT (npi_number) DO UPDATE SET
            name = EXCLUDED.name,
            description = EXCLUDED.description,
            address_1 = EXCLUDED.address_1,
            address_city = EXCLUDED.address_city,
            address_state = EXCLUDED.address_state,
            address_zip = EXCLUDED.address_zip,
            neighborhood = EXCLUDED.neighborhood,
            services = EXCLUDED.services,
            specialties = EXCLUDED.specialties,
            presence_types = EXCLUDED.presence_types,
            professionals = EXCLUDED.professionals,
            embedding = EXCLUDED.embedding,
            embedding_model = EXCLUDED.embedding_model,
            updated_at = NOW()
        """,
        values,
        template="(%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW())",
        page_size=100,
    )

    conn.commit()
    cur.close()
