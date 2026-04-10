"""Generate PDF reports using WeasyPrint with a standalone Jinja2 environment."""

import jinja2
import weasyprint


def generate_calculo_pdf(
    detalle: dict,
    razon_social: str,
    cuit: str,
    periodo_label: str,
    generated_at: str,
) -> bytes:
    """Render the calculo PDF template and return raw PDF bytes.

    Uses its own jinja2.Environment (not Flask's render_template)
    so it can be called from the RQ worker without an app context.
    """
    env = jinja2.Environment(
        loader=jinja2.PackageLoader("website", "templates"),
        autoescape=True,
    )
    template = env.get_template("pdf_calculo.html")
    rendered_html = template.render(
        detalle=detalle,
        razon_social=razon_social,
        cuit=cuit,
        periodo_label=periodo_label,
        generated_at=generated_at,
    )
    return weasyprint.HTML(string=rendered_html).write_pdf()
