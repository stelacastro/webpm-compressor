"""
Bot do Telegram: Conversor de Vídeos/GIFs para Video Stickers WEBM (VP9),
enviados como DOCUMENTO/ANEXO para download (não como sticker renderizado
no chat).

Requisitos técnicos oficiais do Telegram para Video Stickers, garantidos
no processamento (via FFmpeg):
    1. Formato final: .webm com codec de vídeo VP9 (libvpx-vp9).
    2. Áudio: removido completamente (-an), sem nenhuma faixa de áudio.
    3. Dimensões: um dos lados com exatamente 512px, o outro lado com
       512px ou menos, proporção original mantida.
    4. Duração: cortada para no máximo 3 segundos.
    5. FPS: limitado a no máximo 30 quadros por segundo.
    6. Tamanho do arquivo: comprimido iterativamente (ajustando CRF e FPS)
       até ficar abaixo de 256 KB.

Envio: o arquivo .webm final é enviado com reply_document (sendDocument),
mantendo a extensão original, para que o Telegram trate como um anexo
para download em vez de tentar renderizá-lo como sticker no chat.

Dependências:
    pip install python-telegram-bot==21.* python-dotenv
    FFmpeg precisa estar instalado no sistema, acessível no PATH, e
    compilado com suporte a libvpx (codec VP9) para gerar arquivos .webm.

Uso:
    1. Crie um arquivo chamado ".env" na mesma pasta deste script, com o
       seguinte conteúdo (substitua pelo token real do BotFather):

           TELEGRAM_BOT_TOKEN=123456789:ABCdefGhIJKlmNoPQRstuVwxyZ

    2. Rode normalmente:

           python sticker_bot.py

    O token também pode continuar sendo passado como variável de ambiente
    do sistema operacional (ex: $env:TELEGRAM_BOT_TOKEN no PowerShell) —
    nesse caso o arquivo .env é opcional, pois a variável de ambiente tem
    prioridade sobre o valor do .env.
"""

import asyncio
import logging
import os
import subprocess
import tempfile
import uuid

from dotenv import load_dotenv
from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# ---------------------------------------------------------------------------
# Configurações gerais
# ---------------------------------------------------------------------------
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# Carrega variáveis do arquivo ".env" (se existir) para o ambiente do
# processo. Não sobrescreve variáveis de ambiente já definidas manualmente,
# então uma variável de sistema sempre tem prioridade sobre o .env.
load_dotenv()

# Token do bot: lido do ambiente (definido manualmente ou via .env).
BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "COLOQUE_SEU_TOKEN_AQUI")

# Regras exigidas pelo Telegram para video stickers em WEBM (VP9).
MAX_FILE_SIZE_KB = 256
MAX_FILE_SIZE_BYTES = MAX_FILE_SIZE_KB * 1024
MAX_DURATION_SECONDS = 3
MAX_DIMENSION = 512
MAX_FPS = 30
STICKER_EXTENSION = "webm"

# Timeout de segurança para não travar o bot em vídeos problemáticos.
FFMPEG_TIMEOUT_SECONDS = 60


# ---------------------------------------------------------------------------
# Funções auxiliares de processamento com FFmpeg
# ---------------------------------------------------------------------------

def _build_ffmpeg_command(
    input_path: str,
    output_path: str,
    crf: int,
    fps: int,
) -> list:
    """
    Monta o comando FFmpeg com todas as flags necessárias para gerar um
    Video Sticker .webm (VP9) dentro das regras oficiais do Telegram.

    Explicação das flags principais:
      -y                  Sobrescreve o arquivo de saída sem perguntar.
      -i <input>          Arquivo de entrada (vídeo ou GIF).
      -t 3                Corta a duração em no máximo 3 segundos.
      -an                 Remove qualquer faixa de áudio (requisito do Telegram).
      -vf "..."           Filtro de vídeo combinando fps + escala:
                              fps=<fps>          -> limita a taxa de quadros.
                              scale=...:force_original_aspect_ratio=decrease
                                                  -> redimensiona mantendo a
                                                     proporção, garantindo que
                                                     o lado MAIOR fique em
                                                     exatamente 512px e o
                                                     outro lado fique em 512px
                                                     ou menos, sem distorcer.
      -c:v libvpx-vp9     Usa o codec VP9, exigido pelo Telegram para
                          Video Stickers em .webm.
      -pix_fmt yuv420p    Formato de pixel amplamente compatível.
      -crf <crf>          Qualidade constante do VP9 (0-63, quanto menor,
                          melhor a qualidade e maior o arquivo). Ajustado
                          dinamicamente para atingir o limite de 256 KB.
      -b:v 0              Ativa o modo "constant quality" do VP9 (o
                          tamanho é controlado só pelo CRF, sem teto de
                          bitrate artificial).
      -deadline good      Predefinição de velocidade/qualidade do libvpx
                          (bom equilíbrio entre tempo de codificação e
                          compressão).
      -cpu-used 4         Acelera a codificação VP9 mantendo qualidade
                          razoável.
      -row-mt 1           Habilita multithreading por linha (codificação
                          mais rápida em CPUs com vários núcleos).
      -an                 (repetido acima) garante ausência de áudio.
    """
    scale_filter = (
    f"scale='if(gt(iw,ih),{MAX_DIMENSION},-2)':"
    f"'if(gt(iw,ih),-2,{MAX_DIMENSION})'"
)
    vf = f"fps={fps},{scale_filter}"

    return [
        "ffmpeg",
        "-y",
        "-i", input_path,
        "-t", str(MAX_DURATION_SECONDS),
        "-an",
        "-vf", vf,
        "-c:v", "libvpx-vp9",
        "-pix_fmt", "yuv420p",
        "-crf", str(crf),
        "-b:v", "0",
        "-deadline", "good",
        "-cpu-used", "4",
        "-row-mt", "1",
        output_path,
    ]


def _run_ffmpeg(cmd: list) -> None:
    """
    Executa o comando FFmpeg de forma síncrona (chamado dentro de um
    executor separado para não bloquear o loop assíncrono do bot).
    Lança subprocess.CalledProcessError em caso de falha do FFmpeg.
    """
    result = subprocess.run(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=FFMPEG_TIMEOUT_SECONDS,
    )
    if result.returncode != 0:
        stderr_text = result.stderr.decode(errors="ignore")
        raise subprocess.CalledProcessError(
            result.returncode, cmd, output=result.stdout, stderr=stderr_text
        )


def compress_video_to_webm(input_path: str, output_path: str) -> None:
    """
    Converte o vídeo/GIF de entrada em um .webm (VP9) sem áudio, tentando
    várias combinações de CRF e FPS até que o arquivo final fique abaixo
    de 256 KB, sem nunca ultrapassar os limites de duração, dimensão e FPS.

    Estratégia:
      1. Começa com qualidade alta (CRF baixo) e FPS máximo (30).
      2. Se o arquivo ficar grande demais, aumenta o CRF em etapas
         (CRF maior = mais compressão = qualidade menor = arquivo menor).
      3. Se mesmo no CRF máximo o arquivo ainda estiver grande, reduz o
         FPS (menos quadros = menos dados) e repete o processo de CRF.
      4. Se nenhuma combinação atingir o limite, mantém o resultado da
         última tentativa (menor arquivo obtido) e ainda assim entrega,
         registrando um aviso no log.
    """
    crf_steps = [30, 35, 40, 45, 50, 55, 63]
    fps_steps = [MAX_FPS, 24, 20, 15, 12, 10]

    best_size = None
    best_attempt_path = None

    for fps in fps_steps:
        for crf in crf_steps:
            cmd = _build_ffmpeg_command(input_path, output_path, crf, fps)
            _run_ffmpeg(cmd)

            size_bytes = os.path.getsize(output_path)
            logger.info(
                "Tentativa fps=%s crf=%s -> %.1f KB", fps, crf, size_bytes / 1024
            )

            if best_size is None or size_bytes < best_size:
                best_size = size_bytes
                # Guarda uma cópia da melhor tentativa até agora.
                best_attempt_path = output_path + ".best"
                with open(output_path, "rb") as src, open(best_attempt_path, "wb") as dst:
                    dst.write(src.read())

            if size_bytes <= MAX_FILE_SIZE_BYTES:
                return  # Objetivo atingido, output_path já está correto.

    # Se nenhuma combinação atingiu o limite, usa a melhor tentativa
    # registrada (menor tamanho conseguido) como resultado final.
    if best_attempt_path and os.path.exists(best_attempt_path):
        logger.warning(
            "Não foi possível atingir %s KB. Entregando melhor resultado: %.1f KB",
            MAX_FILE_SIZE_KB,
            best_size / 1024,
        )
        with open(best_attempt_path, "rb") as src, open(output_path, "wb") as dst:
            dst.write(src.read())
        os.remove(best_attempt_path)


# ---------------------------------------------------------------------------
# Handlers do Telegram
# ---------------------------------------------------------------------------

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Olá! Envie um vídeo (MP4, MKV, MOV) ou um GIF e eu vou transformá-lo "
        "automaticamente em um Video Sticker .webm (VP9), já dentro de todas "
        "as regras do Telegram (um lado com exatamente 512px, até 3s, até 30 "
        "FPS, sem áudio e menor que 256 KB). O arquivo final é enviado como "
        "um anexo/documento para download, mantendo a extensão .webm."
    )


async def handle_media(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Handler unificado para vídeos, documentos de vídeo e GIFs (animation).
    """
    message = update.message

    # Identifica o objeto de mídia enviado, seja qual for o tipo.
    media_obj = message.video or message.animation or message.document
    if media_obj is None:
        await message.reply_text(
            "Não consegui identificar um vídeo ou GIF válido nessa mensagem."
        )
        return

    # Validação básica de tipo para documentos (evita processar PDFs, etc.).
    if message.document:
        mime_type = message.document.mime_type or ""
        valid_mimes = ("video/", "image/gif")
        if not mime_type.startswith(valid_mimes):
            await message.reply_text(
                "O arquivo enviado não parece ser um vídeo ou GIF válido "
                "(MP4, MKV, MOV ou GIF)."
            )
            return

    status_message = await message.reply_text(
        "⏳ Processando e comprimindo seu sticker..."
    )
    await context.bot.send_chat_action(
        chat_id=message.chat_id, action=ChatAction.UPLOAD_DOCUMENT
    )

    # Cria arquivos temporários exclusivos para esta requisição.
    unique_id = uuid.uuid4().hex
    with tempfile.TemporaryDirectory() as tmp_dir:
        input_path = os.path.join(tmp_dir, f"input_{unique_id}")
        output_path = os.path.join(
            tmp_dir, f"sticker_{unique_id}.{STICKER_EXTENSION}"
        )

        try:
            # Download do arquivo enviado pelo usuário.
            telegram_file = await media_obj.get_file()
            await telegram_file.download_to_drive(custom_path=input_path)

            # Processamento pesado do FFmpeg roda em thread separada para
            # não bloquear o loop de eventos assíncrono do bot.
            await asyncio.to_thread(compress_video_to_webm, input_path, output_path)

            final_size_kb = os.path.getsize(output_path) / 1024

            # Envio explícito como DOCUMENTO (sendDocument), preservando a
            # extensão .webm, para que o Telegram entregue o arquivo como
            # um anexo para download em vez de renderizá-lo como sticker
            # no chat.
            with open(output_path, "rb") as sticker_file:
                await message.reply_document(
                    document=sticker_file,
                    filename=f"sticker.{STICKER_EXTENSION}",
                    caption=(
                        f"✅ Vídeo convertido! ({final_size_kb:.1f} KB)\n"
                        "Formato .webm (VP9), sem áudio, seguindo as regras "
                        "oficiais de Video Sticker do Telegram. Arquivo "
                        "enviado como anexo para download."
                    ),
                )

        except subprocess.TimeoutExpired:
            logger.exception("Timeout ao executar o FFmpeg.")
            await message.reply_text(
                "⚠️ O processamento demorou demais e foi cancelado. Tente "
                "enviar um vídeo mais curto ou mais leve."
            )

        except subprocess.CalledProcessError as error:
            logger.error("Falha no FFmpeg: %s", error.stderr)
            await message.reply_text(
                "⚠️ Não consegui converter esse arquivo. Ele pode estar "
                "corrompido, em um formato não suportado ou o FFmpeg falhou "
                "ao processá-lo."
            )

        except Exception:
            logger.exception("Erro inesperado ao processar mídia.")
            await message.reply_text(
                "⚠️ Ocorreu um erro inesperado ao processar seu arquivo. "
                "Tente novamente em instantes."
            )

        finally:
            # Remove a mensagem de status para manter o chat limpo.
            try:
                await status_message.delete()
            except Exception:
                pass


async def handle_invalid_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Por favor, envie um arquivo de vídeo (MP4, MKV, MOV) ou um GIF "
        "para que eu possa transformá-lo em sticker."
    )


# ---------------------------------------------------------------------------
# Inicialização do bot
# ---------------------------------------------------------------------------

def build_application() -> Application:
    application = ApplicationBuilder().token(BOT_TOKEN).build()

    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("help", start_command))

    # Vídeos enviados como mensagem de vídeo nativa (MP4 comprimido pelo Telegram).
    application.add_handler(MessageHandler(filters.VIDEO, handle_media))

    # GIFs (o Telegram trata GIFs internamente como "animation").
    application.add_handler(MessageHandler(filters.ANIMATION, handle_media))

    # Vídeos enviados como "documento" (sem compressão do Telegram),
    # cobrindo MP4, MKV, MOV e GIFs enviados como arquivo.
    application.add_handler(MessageHandler(filters.Document.VIDEO, handle_media))
    application.add_handler(
        MessageHandler(filters.Document.MimeType("image/gif"), handle_media)
    )

    # Qualquer outro tipo de mensagem recebe uma orientação amigável.
    application.add_handler(
        MessageHandler(~filters.COMMAND & ~filters.VIDEO & ~filters.ANIMATION, handle_invalid_input)
    )

    return application


def main() -> None:
    if not BOT_TOKEN or BOT_TOKEN == "COLOQUE_SEU_TOKEN_AQUI":
        raise RuntimeError(
            "Token do bot não encontrado. Crie um arquivo '.env' na mesma "
            "pasta deste script contendo a linha "
            "TELEGRAM_BOT_TOKEN=seu_token_aqui, ou defina a variável de "
            "ambiente TELEGRAM_BOT_TOKEN antes de executar."
        )

    application = build_application()
    logger.info("Bot iniciado. Aguardando mensagens...")
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()