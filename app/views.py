from rest_framework.views import APIView # type: ignore
from rest_framework.response import Response # type: ignore
from rest_framework import status # type: ignore
from .models import Product
from .serializers import ProductSerializer

class ProductUpdateView(APIView):
    def put(self, request, pk):
        product = Product.objects.get(pk=pk)
        data = request.data.copy()

        # Yangi rasmlarni array qilib qo‘shamiz
        new_images = request.FILES.getlist("images")
        if new_images:
            image_urls = []
            for img in new_images:
                filename = f"static/images/{img.name}"
                with open(filename, "wb+") as f:
                    for chunk in img.chunks():
                        f.write(chunk)
                image_urls.append("/" + filename)

            # Eski rasmlarni saqlab qolamiz + yangilarini qo‘shamiz
            old_images = product.images if isinstance(product.images, list) else []
            data["images"] = old_images + image_urls

        serializer = ProductSerializer(product, data=data, partial=True)
        if serializer.is_valid():
            serializer.save()
            return Response({"success": True, "message": "Product successfully updated", "product": serializer.data})
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)
