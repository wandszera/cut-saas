import time
from app.db.database import SessionLocal

def process_job_pipeline(job_id: int):
    db = SessionLocal()

    try:
        job = db.query(Job).filter(Job.id == job_id).first()
        if not job:
            return

        job.status = "downloading"
        db.commit()
        time.sleep(2)  # Aguarda 2 segundos

        media = download_youtube_media(job.source_value, job.id)

        job.status = "extracting_audio"
        db.commit()
        time.sleep(2)

        audio_path = extract_audio_from_video(job.video_path, job.id)

        job.status = "transcribing"
        db.commit()
        time.sleep(2)

        transcript_path = transcribe_audio(audio_path, job.id)

        job.status = "analyzing"
        db.commit()

        # se quiser pré-calcular algo depois

        job.status = "done"
        db.commit()

    finally:
        db.close()