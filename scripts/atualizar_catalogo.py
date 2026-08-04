import json
import re
from pathlib import Path

import pytesseract
from PIL import Image, ImageEnhance, ImageFilter, ImageOps
from pytesseract import Output


PASTA_IMAGENS = Path("imagens")
ARQUIVO_CATALOGO = Path("fotos.json")
ARQUIVO_REVISAO = Path("revisao.json")

EXTENSOES_PERMITIDAS = {".jpg", ".jpeg", ".png", ".webp"}

PALAVRAS_IGNORADAS = {
    "episódio",
    "episodio",
    "completo",
    "completa",
    "capítulo",
    "capitulo",
    "parte",
    "dublado",
    "legendado",
    "assista",
    "série",
    "serie",
}


def carregar_json(caminho):
    if not caminho.exists():
        return []

    try:
        with caminho.open("r", encoding="utf-8") as arquivo:
            dados = json.load(arquivo)

        return dados if isinstance(dados, list) else []
    except (json.JSONDecodeError, OSError):
        return []


def salvar_json(caminho, dados):
    with caminho.open("w", encoding="utf-8") as arquivo:
        json.dump(dados, arquivo, ensure_ascii=False, indent=2)
        arquivo.write("\n")


def preparar_imagem(caminho):
    imagem = Image.open(caminho).convert("L")

    largura, altura = imagem.size
    if largura < 1600:
        proporcao = 1600 / largura
        imagem = imagem.resize(
            (1600, int(altura * proporcao)),
            Image.Resampling.LANCZOS,
        )

    imagem = ImageOps.autocontrast(imagem)
    imagem = ImageEnhance.Contrast(imagem).enhance(1.8)
    imagem = imagem.filter(ImageFilter.SHARPEN)

    return imagem


def limpar_texto(texto):
    texto = re.sub(r"\s+", " ", texto).strip()
    texto = re.sub(r"[^\wÀ-ÿ'’\-:!? ]", "", texto)
    return texto.strip(" -_|")


def identificar_titulo(caminho):
    imagem = preparar_imagem(caminho)

    dados = pytesseract.image_to_data(
        imagem,
        lang="por+eng",
        config="--oem 3 --psm 11",
        output_type=Output.DICT,
    )

    linhas = {}

    for indice, texto in enumerate(dados["text"]):
        texto = limpar_texto(texto)

        try:
            confianca = float(dados["conf"][indice])
        except (ValueError, TypeError):
            confianca = -1

        if not texto or confianca < 25:
            continue

        chave = (
            dados["block_num"][indice],
            dados["par_num"][indice],
            dados["line_num"][indice],
        )

        if chave not in linhas:
            linhas[chave] = {
                "palavras": [],
                "confiancas": [],
                "topo": dados["top"][indice],
                "altura": dados["height"][indice],
            }

        linhas[chave]["palavras"].append(texto)
        linhas[chave]["confiancas"].append(confianca)

    candidatos = []

    for linha in linhas.values():
        texto = limpar_texto(" ".join(linha["palavras"]))

        if len(texto) < 3:
            continue

        palavras = texto.lower().split()

        if palavras and all(
            palavra in PALAVRAS_IGNORADAS or palavra.isdigit()
            for palavra in palavras
        ):
            continue

        confianca_media = sum(linha["confiancas"]) / len(
            linha["confiancas"]
        )

        quantidade_letras = sum(caractere.isalpha() for caractere in texto)
        tamanho_visual = linha["altura"]
        posicao = linha["topo"] / imagem.height

        pontuacao = confianca_media
        pontuacao += min(quantidade_letras, 35) * 0.8
        pontuacao += min(tamanho_visual, 100) * 0.15

        if 0.08 <= posicao <= 0.8:
            pontuacao += 5

        candidatos.append(
            {
                "texto": texto,
                "confianca_ocr": round(confianca_media, 1),
                "pontuacao": pontuacao,
            }
        )

    candidatos.sort(key=lambda item: item["pontuacao"], reverse=True)

    if not candidatos:
        return {
            "titulo": "",
            "confianca": "baixa",
            "texto_encontrado": "",
        }

    principal = candidatos[0]
    titulo = principal["texto"]
    confianca_ocr = principal["confianca_ocr"]

    if confianca_ocr >= 75 and len(titulo) >= 12 and len(titulo.split()) >= 3:
        confianca = "alta"
    elif confianca_ocr >= 50:
        confianca = "media"
    else:
        confianca = "baixa"

    textos_encontrados = " | ".join(
        candidato["texto"] for candidato in candidatos[:5]
    )

    return {
        "titulo": titulo,
        "confianca": confianca,
        "texto_encontrado": textos_encontrados,
    }


catalogo = carregar_json(ARQUIVO_CATALOGO)
revisao = carregar_json(ARQUIVO_REVISAO)

urls_catalogo = {
    item.get("url")
    for item in catalogo
    if isinstance(item, dict) and item.get("url")
}

revisao_pendente = []

for item in revisao:
    if not isinstance(item, dict):
        continue

    url = item.get("url", "").strip()
    legenda = item.get("legenda", "").strip()

    if url and legenda:
        if url not in urls_catalogo:
            catalogo.append(
                {
                    "url": url,
                    "legenda": legenda,
                }
            )
            urls_catalogo.add(url)

        print(f"Correção publicada: {legenda}")
    else:
        revisao_pendente.append(item)

revisao = revisao_pendente

urls_processadas = {
    item.get("url")
    for item in catalogo + revisao
    if isinstance(item, dict) and item.get("url")
}

novas_imagens = sorted(
    caminho
    for caminho in PASTA_IMAGENS.iterdir()
    if caminho.is_file()
    and caminho.suffix.lower() in EXTENSOES_PERMITIDAS
    and caminho.as_posix() not in urls_processadas
)

for caminho in novas_imagens:
    url = caminho.as_posix()
    print(f"Analisando: {url}")

    try:
        leitura = identificar_titulo(caminho)

        registro = {
            "url": url,
            "legenda": "",
            "confianca": leitura["confianca"],
            "texto_encontrado": leitura["texto_encontrado"],
        }

        if leitura["confianca"] == "alta":
            catalogo.append(
                {
                    "url": url,
                    "legenda": leitura["titulo"],
                }
            )
            print(f"Publicado: {leitura['titulo']}")
        else:
            revisao.append(registro)
            print(f"Enviado para revisão: {leitura['titulo']}")

    except Exception as erro:
        revisao.append(
            {
                "url": url,
                "legenda": "",
                "confianca": "baixa",
                "texto_encontrado": "",
                "erro": str(erro),
            }
        )
        print(f"Erro ao processar {url}: {erro}")

salvar_json(ARQUIVO_CATALOGO, catalogo)
salvar_json(ARQUIVO_REVISAO, revisao)

print(f"Novas imagens analisadas: {len(novas_imagens)}")
print(f"Total publicado no catálogo: {len(catalogo)}")
print(f"Total aguardando revisão: {len(revisao)}")
