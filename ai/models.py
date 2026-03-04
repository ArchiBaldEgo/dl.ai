from django.db import models

class ProgrammingLanguage(models.Model):
    language_name = models.CharField(max_length=255,)

    def __str__(self):
        return self.language_name
    

class Topic(models.Model):
    topic_name = models.CharField(max_length=255)
    programming_language = models.ForeignKey(ProgrammingLanguage, on_delete=models.CASCADE, null = True)  # Добавляем связь с языком программирования

    def __str__(self):
        return self.topic_name
    

# Модель препромпта
class Prompt(models.Model):
    topic = models.ForeignKey(Topic, on_delete=models.CASCADE, null = True)
    prompt_text = models.TextField()
    prompt_name = models.CharField(max_length=255, null = True)
    def __str__(self):
        # Возвращаем только имя промпта вместо полного текста
        return self.prompt_name if self.prompt_name else f"Prompt #{self.id}"    
