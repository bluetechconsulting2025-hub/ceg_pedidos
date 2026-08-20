"""
Leitor de Romaneio (PDF) -> Cadastro no Infor WMS
==================================================
Lê um PDF no formato "LISTA DA PICKING" (SAP), cadastra o receptor
como customer e carrier (é o mesmo dado nesse fluxo) e cria o pedido no Infor WMS com receptor/transportadora e depois cria o romaneio
"""

import streamlit as st
import pdfplumber
import re
import json
import base64
import requests
from io import BytesIO

st.set_page_config(page_title="Leitor de Romaneio PDF", layout="wide")
st.title("📄 Leitor de Romaneio (PDF) → Infor WMS")

# ============================
# CONFIGURAÇÕES DO SISTEMA
# ============================
WAREHOUSE_MAP = {
    "RIO II": "BLUELOGISTICA_PRD_BLUELOGISTICA_PRD_SCE_PRD_0_wmwhse2",
    "SP I": "BLUELOGISTICA_PRD_BLUELOGISTICA_PRD_SCE_PRD_0_wmwhse4",
}
WAREHOUSE_CUSTOMERS = "BLUELOGISTICA_PRD_ENTERPRISE"
BASE_URL = "https://mingle-ionapi.inforcloudsuite.com/BLUELOGISTICA_PRD/WM/wmwebservice_rest"
TOKEN_URL = "https://mingle-sso.inforcloudsuite.com:443/BLUELOGISTICA_PRD/as/token.oauth2"

CLIENT_ID = st.secrets["CLIENT_ID"]
CLIENT_SECRET = st.secrets["CLIENT_SECRET"]
USERNAME = st.secrets["USERNAME"]
PASSWORD = st.secrets["PASSWORD"]


def gerar_token():
    auth_string = f"{CLIENT_ID}:{CLIENT_SECRET}"
    auth_base64 = base64.b64encode(auth_string.encode()).decode()
    headers = {
        "Authorization": f"Basic {auth_base64}",
        "Content-Type": "application/x-www-form-urlencoded",
    }
    payload = {"grant_type": "password", "username": USERNAME, "password": PASSWORD}
    resp = requests.post(TOKEN_URL, data=payload, headers=headers)
    if resp.status_code != 200:
        return None
    return resp.json().get("access_token")


def postar_customer(receptor_codigo, receptor_nome, headers):
    payload = {
        "storerkey": receptor_codigo[:30],
        "company": receptor_nome[:30] if receptor_nome else None,
        "type": "2",
    }
    endpoint = f"{BASE_URL}/{WAREHOUSE_CUSTOMERS}/customers"
    resp = requests.post(endpoint, headers=headers, json=payload)
    return {"endpoint": endpoint, "payload_enviado": payload, "status": resp.status_code, "resposta": resp.text}


def postar_carrier(receptor_codigo, receptor_nome, headers):
    payload = {
        "storerkey": receptor_codigo[:30],
        "company": receptor_nome[:30] if receptor_nome else None,
        "type": "3",
    }
    endpoint = f"{BASE_URL}/{WAREHOUSE_CUSTOMERS}/carriers"
    resp = requests.post(endpoint, headers=headers, json=payload)
    return {"endpoint": endpoint, "payload_enviado": payload, "status": resp.status_code, "resposta": resp.text}


def postar_shipment(warehouse, shipment_json, headers):
    endpoint = f"{BASE_URL}/{warehouse}/shipments"
    resp = requests.post(endpoint, headers=headers, json=shipment_json)
    return {"endpoint": endpoint, "payload_enviado": shipment_json, "status": resp.status_code, "resposta": resp.text}


def liberar_shipment(warehouse, orderkey, headers):
    endpoint = f"{BASE_URL}/{warehouse}/shipments/{orderkey}/release"
    resp = requests.post(endpoint, headers=headers)
    return {"endpoint": endpoint, "status": resp.status_code, "resposta": resp.text}


STATUS_MAP = {
    "00": "Ordem em branco", "02": "Criado extern.", "04": "Criado intern.",
    "06": "Não alocou", "08": "Convertido", "09": "Não inic.", "-1": "Desc.",
    "10": "Agrupado", "11": "Volume pré-alocado", "12": "Pré-alocado",
    "13": "Liberado p/ planej. de armaz.", "14": "Volume alocado",
    "15": "Volume aloc./volume sep.", "16": "Volume alocado/volume exp.",
    "17": "Alocado", "18": "Substituído", "-2": "SemSincronismo",
    "22": "Volume liberado", "25": "Volume liberado/volume sep.",
    "27": "Volume liberado/volume exp.", "29": "Liberado", "51": "Em separação",
    "52": "Vol. sep.", "53": "Vol. separado/volume exp.", "55": "Separação concluída",
    "57": "Separado/volume exp.", "61": "Em emb.", "68": "Emb. concluída",
    "75": "Preparado", "78": "Manifestado", "82": "Em carreg.", "88": "Carregado",
    "92": "Volume expedido", "94": "Fechar produção", "95": "Expedição concluída",
    "96": "Entrega aceita", "97": "Entrega recusada", "98": "Cancelado extern.",
    "99": "Cancelado intern.",
}


def consultar_status_pedido(warehouse, orderkey, headers):
    endpoint = f"{BASE_URL}/{warehouse}/shipments/{orderkey}"
    resp = requests.get(endpoint, headers=headers)
    if resp.status_code not in (200, 201):
        return {"liberado": False, "erro": f"Não foi possível consultar o pedido (HTTP {resp.status_code})", "linhas_pendentes": []}
    try:
        data = resp.json()
    except Exception:
        return {"liberado": False, "erro": "Resposta inválida do Infor", "linhas_pendentes": []}
    status_header = str(data.get("status", ""))
    if status_header == "29":
        return {"liberado": True, "status_header": status_header, "status_desc": STATUS_MAP.get(status_header, status_header), "linhas_pendentes": []}
    linhas_pendentes = []
    for det in data.get("orderdetails", []):
        s = str(det.get("status", ""))
        if s != "29":
            linhas_pendentes.append({
                "sku": det.get("sku"),
                "status": s,
                "status_desc": STATUS_MAP.get(s, s),
                "openqty": det.get("openqty"),
                "uom": det.get("uom"),
                "linha": det.get("orderlinenumber"),
            })
    return {
        "liberado": False,
        "status_header": status_header,
        "status_desc": STATUS_MAP.get(status_header, status_header),
        "linhas_pendentes": linhas_pendentes,
    }


def postar_load(warehouse, orderkey, storerkey, receptor_nome, headers):
    hoje = __import__("datetime").date.today().strftime("%Y%m%d")
    receptor_limpo = re.sub(r"[^0-9A-Za-z]", "", receptor_nome or "LOAD")
    externalid = f"{receptor_limpo[:12]}{hoje}"[:20]
    payload = {
        "externalid": externalid,
        "stops": [
            {
                "stop": 1,
                "loadorderdetails": [{"shipmentorderid": orderkey, "storer": storerkey}],
            }
        ],
        "route": (receptor_nome or "LOAD")[:10],
    }
    endpoint = f"{BASE_URL}/{warehouse}/loads"
    resp = requests.post(endpoint, headers=headers, json=payload)
    return {"endpoint": endpoint, "payload_enviado": payload, "status": resp.status_code, "resposta": resp.text}

# ============================
# PARSER
# ============================
def extrair_texto(pdf_bytes: bytes) -> str:
    with pdfplumber.open(BytesIO(pdf_bytes)) as pdf:
        return "\n".join(page.extract_text() or "" for page in pdf.pages)


def parse_romaneio(texto: str) -> dict:
    # ---- Cabeçalho ----
    entrega   = re.search(r"Entrega\s*:\s*(\d+)", texto)
    centro    = re.search(r"Centro\s*:\s*(\S+)", texto)
    deposito  = re.search(r"Dep[oó]sito\s*:\s*(\S+)", texto)
    sociedade = re.search(r"Sociedade\s+(\d+)\s+(.+?)(?:\n|$)", texto)
    receptor  = re.search(r"Receptor\s*:\s*(\d+)\s+(.+?)(?:\n|$)", texto)
    observ    = re.search(r"Observ\.?\s*:\s*(.+?)(?:\n|$)", texto)

    # ---- Itens ----
    orderdetails = []
    for linha in texto.splitlines():
        tokens = linha.split()
        if len(tokens) < 4:
            continue
        # material: código numérico de 8 dígitos no início da linha
        if not re.fullmatch(r"\d{8}", tokens[0]):
            continue
        # unidade: último token (letras)
        unidade = tokens[-1]
        # quantidade: penúltimo token, formato 0,000
        qtd_txt = tokens[-2]
        if not re.fullmatch(r"[\d\.]+,\d+", qtd_txt):
            continue
        localizacao = tokens[-3]
        descricao = " ".join(tokens[1:-3])
        try:
            quantidade = float(qtd_txt.replace(".", "").replace(",", "."))
        except ValueError:
            quantidade = 0.0

        orderdetails.append({
            "sku": tokens[0].lstrip("0") or tokens[0],
            "descricao": descricao,
            "localizacao": localizacao,
            "openqty": quantidade,
            "uom": unidade,
        })

    return {
        "orderkey": entrega.group(1) if entrega else None,
        "centro": centro.group(1) if centro else None,
        "deposito": deposito.group(1) if deposito else None,
        "sociedade_codigo": sociedade.group(1) if sociedade else None,
        "sociedade_nome": sociedade.group(2).strip() if sociedade else None,
        "receptor_codigo": receptor.group(1) if receptor else None,
        "receptor_nome": receptor.group(2).strip() if receptor else None,
        "observacao": observ.group(1).strip() if observ else None,
        "orderdetails": orderdetails,
    }

STORER_MAP = {
    "0040": "GNSPS",
    "0017": "CEGRJ",
    "0016": "CEG",
}


# ============================
# UI
# ============================
planta = st.selectbox("Planta", list(WAREHOUSE_MAP.keys()))


arquivo = st.file_uploader("Envie o PDF do romaneio", type=["pdf"])

if arquivo:
    texto = extrair_texto(arquivo.getvalue())
    dados = parse_romaneio(texto)
    storerkey_auto = STORER_MAP.get(dados["sociedade_codigo"], "")
    
    st.subheader("Cabeçalho identificado")
    col1, col2, col3 = st.columns(3)
    col1.metric("Entrega (orderkey)", dados["orderkey"] or "—")
    col2.metric("Centro", dados["centro"] or "—")
    col3.metric("Depósito", dados["deposito"] or "—")
    st.markdown(f"**Sociedade:** {dados['sociedade_codigo'] or '—'} — {dados['sociedade_nome'] or '—'}")
    st.markdown(f"**Receptor / Transportadora:** {dados['receptor_codigo'] or '—'} — {dados['receptor_nome'] or '—'}")
    st.markdown(f"**Observação:** {dados['observacao'] or '—'}")

    st.subheader(f"Itens ({len(dados['orderdetails'])})")
    st.dataframe(dados["orderdetails"], use_container_width=True)

    # ---- Monta JSON no padrão de shipment do Infor ----
    # receptor é usado como consigneekey (cliente final) e carriercode
    # (transportadora), já que nesse fluxo é o mesmo dado
    shipment_json = {
        "storerkey": storerkey_auto,
        "orderkey": dados["orderkey"],
        "consigneekey": dados["receptor_codigo"],
        "carriercode": dados["receptor_codigo"],
        "orderdetails": [
            {"sku": item["sku"], "openqty": item["openqty"], "uom": item["uom"]}
            for item in dados["orderdetails"]
        ],
    }

    st.subheader("JSON do Shipment")
    st.json(shipment_json)

    st.download_button(
        "⬇️ Baixar JSON",
        data=json.dumps(shipment_json, ensure_ascii=False, indent=2),
        file_name=f"shipment_{dados['orderkey'] or 'romaneio'}.json",
        mime="application/json",
    )

    st.markdown("---")
    if st.button("🚀 Cadastrar receptor/transportadora e criar shipment no Infor"):
        if not dados["receptor_codigo"]:
            st.error("Não consegui identificar o receptor no PDF — não dá pra cadastrar.")
        elif not storerkey_manual:
            st.error("Informe o storerkey antes de enviar.")
        else:
            with st.spinner("Autenticando no Infor..."):
                token = gerar_token()
            if not token:
                st.error("Falha ao gerar token no Infor. Confira as credenciais em st.secrets.")
            else:
                headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
                warehouse = WAREHOUSE_MAP[planta]

                res_customer = postar_customer(dados["receptor_codigo"], dados["receptor_nome"], headers)
                res_carrier = postar_carrier(dados["receptor_codigo"], dados["receptor_nome"], headers)
                res_shipment = postar_shipment(warehouse, shipment_json, headers)

                res_release = None
                status_pedido = None
                res_load = None
                if res_shipment["status"] in (200, 201) and dados["orderkey"]:
                    res_release = liberar_shipment(warehouse, dados["orderkey"], headers)
                    status_pedido = consultar_status_pedido(warehouse, dados["orderkey"], headers)
                    res_load = postar_load(
                        warehouse, dados["orderkey"], storerkey_manual, dados["receptor_nome"], headers
                    )

                st.subheader("Resultado")
                for nome, res in [
                    ("Receptor (customer)", res_customer),
                    ("Transportadora (carrier)", res_carrier),
                    ("Shipment", res_shipment),
                ] + ([("Release", res_release)] if res_release else []) + (
                    [("Load (romaneio)", res_load)] if res_load else []
                ):
                    ok = res["status"] in (200, 201)
                    icone = "✅" if ok else "❌"
                    with st.expander(f"{icone} {nome} — HTTP {res['status']}", expanded=not ok):
                        st.json(res)

                if status_pedido:
                    st.markdown("---")
                    if "erro" in status_pedido:
                        st.warning(f"⚠️ Não foi possível consultar o status: {status_pedido['erro']}")
                    elif status_pedido["liberado"]:
                        st.success("✅ Pedido totalmente liberado no Infor!")
                    else:
                        status_label = f"{status_pedido.get('status_header', '?')} — {status_pedido.get('status_desc', '')}"
                        st.warning(f"⚠️ Pedido não está totalmente liberado (status atual: **{status_label}**)")
                        linhas = status_pedido.get("linhas_pendentes", [])
                        if linhas:
                            st.markdown("**Linhas pendentes:**")
                            st.dataframe(linhas, use_container_width=True)
else:
    st.info("Envie um PDF de romaneio para começar.")
