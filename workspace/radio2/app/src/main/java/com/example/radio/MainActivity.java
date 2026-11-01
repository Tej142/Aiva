package com.example.radio;

import android.os.Bundle;

import androidx.activity.EdgeToEdge;
import androidx.appcompat.app.AppCompatActivity;
import androidx.core.graphics.Insets;
import androidx.core.view.ViewCompat;
import androidx.core.view.WindowInsetsCompat;
import android.widget.*;
import android.view.View;

public class MainActivity extends AppCompatActivity {

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        EdgeToEdge.enable(this);
        setContentView(R.layout.activity_main);
        Button btn = findViewById(R.id.btn);
        RadioGroup rg = findViewById(R.id.g);

        rg.setOnCheckedChangeListener((group,checkedId)-> {
           int id = rg.getCheckedRadioButtonId();
           if(id == -1){
               Toast.makeText(this, "Select any", Toast.LENGTH_LONG).show();
           }
           else{
                RadioButton rb = findViewById(id);
                Toast.makeText(this, rb.getText(),Toast.LENGTH_LONG).show();
           }
        });
    }
}