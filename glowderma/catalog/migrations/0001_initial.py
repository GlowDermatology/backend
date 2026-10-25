"""catalog.0001_initial — Category, Product (stored generated search_text + GIN trgm index), ProductStock,
ProductImage (deferrable unique position), Banner (design 01 §4, §6.2, §7)."""

import django.contrib.postgres.indexes
import django.db.models.constraints
import django.db.models.deletion
import django.db.models.functions.comparison
import django.db.models.functions.text
from django.db import migrations, models

import glowderma.catalog.media
import glowderma.catalog.models
import glowderma.core.models


class Migration(migrations.Migration):
    initial = True

    # core.0007 creates pg_trgm (the GIN trigram index below) and the glowderma_ci collation the selectors
    # use; accounts.0001 holds StaffUser (last_change_actor, uploaded_by). Never a later accounts migration:
    # those are created by a parallel package (design 01 §7 names accounts.0004, see the B2 report).
    dependencies = [
        ("core", "0007_db_extensions"),
        ("accounts", "0001_initial"),
    ]

    operations = [
        migrations.CreateModel(
            name="Category",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                (
                    "created_at",
                    models.DateTimeField(default=glowderma.core.models.timestamp_default, editable=False),
                ),
                (
                    "updated_at",
                    models.DateTimeField(default=glowderma.core.models.timestamp_default),
                ),
                ("slug", models.SlugField(max_length=40, unique=True)),
                ("name_uz", models.CharField(max_length=60)),
                ("name_ru", models.CharField(blank=True, max_length=60)),
                ("name_en", models.CharField(blank=True, max_length=60)),
                ("position", models.PositiveIntegerField(default=0)),
                ("is_active", models.BooleanField(default=True)),
                (
                    "image",
                    models.ImageField(
                        blank=True,
                        max_length=255,
                        upload_to=glowderma.catalog.media.category_image_path,
                    ),
                ),
            ],
            options={
                "verbose_name_plural": "categories",
                "ordering": ["position", "id"],
                "indexes": [models.Index(fields=["position", "id"], name="catalog_category_position")],
                "constraints": [
                    models.CheckConstraint(
                        condition=models.Q(("slug__regex", "^[a-z0-9-]{2,40}$")),
                        name="category_slug_format",
                    ),
                    models.CheckConstraint(
                        condition=models.Q(("slug", "order"), _negated=True),
                        name="category_slug_not_reserved",
                    ),
                    models.CheckConstraint(
                        condition=models.Q(("name_uz", ""), _negated=True),
                        name="category_name_uz_required",
                    ),
                ],
            },
        ),
        migrations.CreateModel(
            name="Product",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                (
                    "created_at",
                    models.DateTimeField(default=glowderma.core.models.timestamp_default, editable=False),
                ),
                (
                    "updated_at",
                    models.DateTimeField(default=glowderma.core.models.timestamp_default),
                ),
                ("billz_id", models.CharField(max_length=14, unique=True)),
                ("billz_name", models.CharField(blank=True, max_length=255)),
                ("volume", models.CharField(blank=True, max_length=40)),
                ("billz_price", models.BigIntegerField(null=True)),
                ("billz_synced_at", models.DateTimeField(null=True)),
                ("price", models.BigIntegerField(default=0)),
                ("promo", models.BooleanField(default=False)),
                (
                    "status",
                    models.CharField(
                        choices=[("active", "Active"), ("hidden", "Hidden")],
                        db_index=True,
                        default="hidden",
                        max_length=8,
                    ),
                ),
                ("is_new_from_billz", models.BooleanField(default=False)),
                ("name_uz", models.CharField(blank=True, max_length=120)),
                ("name_ru", models.CharField(blank=True, max_length=120)),
                ("name_en", models.CharField(blank=True, max_length=120)),
                ("description_uz", models.TextField(blank=True)),
                ("description_ru", models.TextField(blank=True)),
                ("description_en", models.TextField(blank=True)),
                ("ingredients_uz", models.TextField(blank=True)),
                ("ingredients_ru", models.TextField(blank=True)),
                ("ingredients_en", models.TextField(blank=True)),
                ("usage_uz", models.TextField(blank=True)),
                ("usage_ru", models.TextField(blank=True)),
                ("usage_en", models.TextField(blank=True)),
                (
                    "search_text",
                    models.GeneratedField(
                        db_persist=True,
                        expression=django.db.models.functions.text.Lower(
                            glowderma.catalog.models.JoinText(
                                models.F("name_uz"),
                                models.Value(" "),
                                models.F("name_ru"),
                                models.Value(" "),
                                models.F("name_en"),
                                models.Value(" "),
                                models.F("billz_name"),
                                models.Value(" "),
                                models.F("billz_id"),
                            )
                        ),
                        output_field=models.TextField(),
                    ),
                ),
                ("last_change_at", models.DateTimeField(null=True)),
                (
                    "last_change_kind",
                    models.CharField(
                        blank=True,
                        choices=[
                            ("created", "Created"),
                            ("price_changed", "Price Changed"),
                            ("activated", "Activated"),
                            ("hidden", "Hidden"),
                            ("promo_added", "Promo Added"),
                            ("promo_removed", "Promo Removed"),
                            ("updated", "Updated"),
                        ],
                        max_length=16,
                    ),
                ),
                ("last_change_old_price", models.BigIntegerField(null=True)),
                ("last_change_new_price", models.BigIntegerField(null=True)),
                (
                    "category",
                    models.ForeignKey(
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="products",
                        to="catalog.category",
                    ),
                ),
                (
                    "last_change_actor",
                    models.ForeignKey(
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="+",
                        to="accounts.staffuser",
                    ),
                ),
            ],
            options={
                "ordering": ["-id"],
            },
        ),
        migrations.CreateModel(
            name="Banner",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                (
                    "created_at",
                    models.DateTimeField(default=glowderma.core.models.timestamp_default, editable=False),
                ),
                (
                    "updated_at",
                    models.DateTimeField(default=glowderma.core.models.timestamp_default),
                ),
                ("title_uz", models.CharField(max_length=80)),
                ("title_ru", models.CharField(blank=True, max_length=80)),
                ("title_en", models.CharField(blank=True, max_length=80)),
                (
                    "image",
                    models.ImageField(
                        height_field="image_height",
                        max_length=255,
                        upload_to=glowderma.catalog.media.banner_image_path,
                        width_field="image_width",
                    ),
                ),
                ("image_width", models.PositiveIntegerField()),
                ("image_height", models.PositiveIntegerField()),
                (
                    "link_kind",
                    models.CharField(
                        choices=[
                            ("none", "None"),
                            ("product", "Product"),
                            ("category", "Category"),
                        ],
                        default="none",
                        max_length=8,
                    ),
                ),
                ("is_active", models.BooleanField(default=True)),
                ("starts_at", models.DateTimeField(null=True)),
                ("ends_at", models.DateTimeField(null=True)),
                ("position", models.PositiveIntegerField(default=0)),
                (
                    "link_category",
                    models.ForeignKey(
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="banners",
                        to="catalog.category",
                    ),
                ),
                (
                    "link_product",
                    models.ForeignKey(
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="banners",
                        to="catalog.product",
                    ),
                ),
            ],
            options={
                "ordering": ["position", "id"],
            },
        ),
        migrations.CreateModel(
            name="ProductImage",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                (
                    "created_at",
                    models.DateTimeField(default=glowderma.core.models.timestamp_default, editable=False),
                ),
                (
                    "updated_at",
                    models.DateTimeField(default=glowderma.core.models.timestamp_default),
                ),
                (
                    "file",
                    models.ImageField(
                        height_field="height",
                        max_length=255,
                        upload_to=glowderma.catalog.media.product_image_path,
                        width_field="width",
                    ),
                ),
                ("position", models.PositiveSmallIntegerField()),
                ("width", models.PositiveIntegerField()),
                ("height", models.PositiveIntegerField()),
                ("content_type", models.CharField(max_length=32)),
                ("size_bytes", models.PositiveIntegerField()),
                ("sha256", models.CharField(max_length=64)),
                (
                    "product",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="images",
                        to="catalog.product",
                    ),
                ),
                (
                    "uploaded_by",
                    models.ForeignKey(
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="+",
                        to="accounts.staffuser",
                    ),
                ),
            ],
            options={
                "ordering": ["position", "id"],
            },
        ),
        migrations.CreateModel(
            name="ProductStock",
            fields=[
                (
                    "product",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.CASCADE,
                        primary_key=True,
                        related_name="stock",
                        serialize=False,
                        to="catalog.product",
                    ),
                ),
                ("quantity", models.IntegerField(default=0)),
                ("synced_at", models.DateTimeField(null=True)),
                ("billz_updated_at", models.DateTimeField(null=True)),
            ],
            options={
                "indexes": [
                    models.Index(
                        condition=models.Q(("quantity__lte", 0)),
                        fields=["quantity"],
                        name="catalog_stock_out",
                    )
                ],
            },
        ),
        migrations.AddIndex(
            model_name="product",
            index=django.contrib.postgres.indexes.GinIndex(
                fields=["search_text"],
                name="catalog_product_search_trgm",
                opclasses=["gin_trgm_ops"],
            ),
        ),
        migrations.AddIndex(
            model_name="product",
            index=models.Index(fields=["category", "status"], name="catalog_product_cat_status"),
        ),
        migrations.AddIndex(
            model_name="product",
            index=models.Index(
                models.OrderBy(
                    django.db.models.functions.comparison.Coalesce("last_change_at", "created_at"),
                    descending=True,
                ),
                models.OrderBy(models.F("id"), descending=True),
                name="catalog_product_default_order",
            ),
        ),
        migrations.AddIndex(
            model_name="product",
            index=models.Index(
                condition=models.Q(("is_new_from_billz", True)),
                fields=["is_new_from_billz"],
                name="catalog_product_new",
            ),
        ),
        migrations.AddConstraint(
            model_name="product",
            constraint=models.CheckConstraint(
                condition=models.Q(("billz_id__regex", "^\\d{6,14}$")),
                name="product_billz_id_format",
            ),
        ),
        migrations.AddConstraint(
            model_name="product",
            constraint=models.CheckConstraint(
                condition=models.Q(("price__gte", 0)), name="product_price_non_negative"
            ),
        ),
        migrations.AddConstraint(
            model_name="product",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    ("billz_price__isnull", True),
                    ("billz_price__gte", 0),
                    _connector="OR",
                ),
                name="product_billz_price_non_negative",
            ),
        ),
        migrations.AddConstraint(
            model_name="product",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    models.Q(
                        ("last_change_actor__isnull", True),
                        ("last_change_at__isnull", True),
                        ("last_change_kind", ""),
                        ("last_change_new_price__isnull", True),
                        ("last_change_old_price__isnull", True),
                    ),
                    models.Q(
                        ("last_change_at__isnull", False),
                        models.Q(("last_change_kind", ""), _negated=True),
                    ),
                    _connector="OR",
                ),
                name="product_last_change_all_or_nothing",
            ),
        ),
        migrations.AddConstraint(
            model_name="product",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    models.Q(("last_change_kind", "price_changed"), _negated=True),
                    models.Q(
                        ("last_change_new_price__isnull", False),
                        ("last_change_old_price__isnull", False),
                    ),
                    _connector="OR",
                ),
                name="product_price_change_has_prices",
            ),
        ),
        migrations.AddIndex(
            model_name="banner",
            index=models.Index(fields=["position", "id"], name="catalog_banner_position"),
        ),
        migrations.AddConstraint(
            model_name="banner",
            constraint=models.CheckConstraint(
                condition=models.Q(("title_uz", ""), _negated=True),
                name="banner_title_uz_required",
            ),
        ),
        migrations.AddConstraint(
            model_name="banner",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    ("starts_at__isnull", True),
                    ("ends_at__isnull", True),
                    ("ends_at__gt", models.F("starts_at")),
                    _connector="OR",
                ),
                name="banner_ends_after_starts",
            ),
        ),
        migrations.AddConstraint(
            model_name="banner",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    models.Q(
                        ("link_category__isnull", True),
                        ("link_kind", "none"),
                        ("link_product__isnull", True),
                    ),
                    models.Q(("link_category__isnull", True), ("link_kind", "product")),
                    models.Q(("link_kind", "category"), ("link_product__isnull", True)),
                    _connector="OR",
                ),
                name="banner_link_kind_targets",
            ),
        ),
        migrations.AddConstraint(
            model_name="productimage",
            constraint=models.UniqueConstraint(
                deferrable=django.db.models.constraints.Deferrable["DEFERRED"],
                fields=("product", "position"),
                name="productimage_product_position_unique",
            ),
        ),
        migrations.AddConstraint(
            model_name="productimage",
            constraint=models.CheckConstraint(
                condition=models.Q(("position__lte", 7)),
                name="productimage_position_max_8",
            ),
        ),
        migrations.AddConstraint(
            model_name="productimage",
            constraint=models.CheckConstraint(
                condition=models.Q(("content_type__in", ["image/jpeg", "image/png", "image/webp"])),
                name="productimage_content_type",
            ),
        ),
    ]
