# MongoDB document structure (no ORM)
# Collection: 'plan_overrides'
# Fields: id (str uuid), plan_key (str), monthly_price_usd (float|None),
#         annual_price_usd (float|None), connected_accounts (int|None),
#         posts_per_month (int|None), ai_text_per_month (int|None),
#         ai_images_per_month (int|None), ai_videos_per_month (int|None),
#         templates (int|None), brand_voice (bool|None),
#         approval_workflow (bool|None), created_at (datetime), updated_at (datetime)
#
# Collection: 'plans_ai_usage'
# Fields: id (str uuid), workspace_id (str), user_id (str|None),
#         kind (str: text|image|video), quantity (int), metadata_json (str|None),
#         created_at (datetime)
