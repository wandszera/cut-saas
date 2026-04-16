from sqlalchemy import Column, Integer, String, DateTime, Text
from sqlalchemy.sql import func
from app.db.database import Base


class Job(Base):
    __tablename__ = "jobs"

    id = Column(Integer, primary_key=True, index=True)
    source_type = Column(String, nullable=False)
    source_value = Column(Text, nullable=False)
    status = Column(String, default="pending", nullable=False)

    title = Column(String, nullable=True)

    video_path = Column(String, nullable=True)
    audio_path = Column(String, nullable=True)
    transcript_path = Column(String, nullable=True)
    result_path = Column(String, nullable=True)

    detected_niche = Column(String, nullable=True)
    niche_confidence = Column(String, nullable=True)

    error_message = Column(Text, nullable=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    @property
    def status_label(self):
        labels = {
            "pending": "Na fila",
            "downloading": "Baixando...",
            "extracting_audio": "Extraindo áudio...",
            "transcribing": "Transcrevendo...",
            "analyzing": "Analisando...",
            "rendering": "Renderizando...",
            "done": "Concluído",
            "failed": "Erro"
        }
        return labels.get(self.status, self.status)

    @property
    def progress(self):
        progress_map = {
            "pending": 5,
            "downloading": 20,
            "extracting_audio": 40,
            "transcribing": 70,
            "analyzing": 85,
            "rendering": 95,
            "done": 100,
            "failed": 0
        }
        return progress_map.get(self.status, 0)