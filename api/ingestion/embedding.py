from embeddings import generate_embedding, get_embedding_model_version


def build_embedding_input(practice: dict) -> str:
    parts = [practice["description"]]
    if practice["services"]:
        parts.append(f"Services: {', '.join(practice['services'])}")
    if practice["professionals"]:
        roster = ", ".join(
            f"{p['name']} {p.get('credentials', '')}".strip()
            for p in practice["professionals"]
        )
        parts.append(f"Professionals: {roster}")
    return ". ".join(parts)


def embed_practices(practices: list[dict]) -> None:
    model_version = get_embedding_model_version()
    batch_size = 50
    for i in range(0, len(practices), batch_size):
        batch = practices[i:i + batch_size]
        for practice in batch:
            practice["embedding"] = generate_embedding(build_embedding_input(practice))
            practice["embedding_model"] = model_version
        if (i + batch_size) % 100 == 0 or i + batch_size >= len(practices):
            print(f"  Embedded {min(i + batch_size, len(practices))}/{len(practices)} practices.")
